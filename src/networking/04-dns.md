# Part 4 — DNS in Depth

"It's always DNS" is a joke because it is so often true. DNS is a globally distributed, heavily cached database that every connection depends on, and caching means mistakes can persist for hours after they are fixed. This Part covers how resolution works, record types, TTLs and caching, split-horizon DNS, cloud routing policies, Kubernetes DNS gotchas, and how to debug all of it with `dig`.

## 4.1 What DNS Does

DNS (Domain Name System) maps names to data, most commonly to IP addresses. It is:

- **Hierarchical**: the namespace is a tree rooted at `.`.
- **Distributed**: each zone is served by its own authoritative servers.
- **Cached**: answers are cached at every level for the record's TTL.
- **Mostly UDP on port 53**: TCP 53 is used for responses too big for UDP (truncation), zone transfers, and increasingly for DNS over TLS (853) and DNS over HTTPS (443).

### 4.1.1 The namespace

```text
                              . (root)
               _______________|________________
              |               |                |
             com             org              io           <- TLDs
          ____|____           |                |
         |         |       wikipedia         github
      example    amazon       |
         |                   en
        www                                                   <- labels

FQDN: www.example.com.   (the trailing dot is the root; usually implicit)
```

A **zone** is a portion of the namespace managed as a unit. `example.com` can delegate `dev.example.com` to a different set of name servers, making it a separate zone.

## 4.2 The Resolution Path

### 4.2.1 The players

| Component | Role | Examples |
|-----------|------|----------|
| Stub resolver | Library in the OS that sends queries; does not recurse | glibc, `systemd-resolved`, musl |
| Recursive resolver | Does the legwork, caches results | ISP resolver, 8.8.8.8, 1.1.1.1, AWS Route 53 Resolver (VPC+2), CoreDNS (for cluster zones and forwarding) |
| Root servers | Know where TLD servers are | 13 named letters (a–m), hundreds of anycast instances |
| TLD servers | Know authoritative servers for each domain | Verisign for `.com` |
| Authoritative servers | Hold the actual records for a zone | Route 53, Cloudflare, BIND |

### 4.2.2 A full recursive lookup

```text
 App          Stub           Recursive resolver      Root      .com TLD     example.com auth
  |            |                    |                  |           |               |
  |getaddrinfo |                    |                  |           |               |
  |----------->| A www.example.com? |                  |           |               |
  |            |------------------->| (cache miss)     |           |               |
  |            |                    |--- query ------->|           |               |
  |            |                    |<-- referral: .com NS --------|               |
  |            |                    |--- query ------------------->|               |
  |            |                    |<-- referral: example.com NS -|               |
  |            |                    |--- query ------------------------------------>|
  |            |                    |<-- A 93.184.215.14 (TTL 300), authoritative ---|
  |            |<-- answer (cached for 300 s) ---|          |               |
  |<-----------|                    |                  |           |               |
```

Key points:

- The stub sends a **recursive** query (RD flag set): "give me the final answer".
- The recursive resolver sends **iterative** queries to root, TLD, authoritative, following referrals.
- **Glue records**: if `example.com`'s name server is `ns1.example.com`, the TLD must include its IP in the referral or you get a chicken-and-egg loop.
- The resolver caches every step (root and TLD NS records are cached for days), so most lookups only need the final hop or no hop at all.

### 4.2.3 What happens on the host before DNS

Linux does not go straight to DNS. `getaddrinfo()` follows `/etc/nsswitch.conf`:

```text
hosts: files dns
```

So `/etc/hosts` wins first. Then the resolver configuration in `/etc/resolv.conf`:

```text
nameserver 127.0.0.53
options edns0 trust-ad
search ec2.internal corp.example.com
```

On systemd-based distros, `127.0.0.53` is `systemd-resolved`, a local caching stub. The real upstream servers are shown by:

```bash
resolvectl status
```

```text
Link 2 (ens5)
    Current Scopes: DNS
         Protocols: +DefaultRoute -LLMNR -mDNS -DNSOverTLS DNSSEC=no/unsupported
Current DNS Server: 10.0.0.2
       DNS Servers: 10.0.0.2
        DNS Domain: ec2.internal
```

> **Warning:** `dig` and `nslookup` talk to DNS servers directly and **bypass** `/etc/hosts` and nsswitch. Your application uses `getaddrinfo()`. To see what an application would get, use `getent ahosts www.example.com`. Many "DNS works with dig but the app fails" mysteries are an `/etc/hosts` entry or a search-domain issue.

### 4.2.4 Search domains and ndots

If a name has fewer dots than `ndots` (default 1), the resolver tries appending each search domain first. With `search corp.example.com` and a query for `db`, it tries `db.corp.example.com` and then `db.`. This is convenient and also the source of the famous Kubernetes `ndots:5` performance issue (Section 4.7).

## 4.3 Record Types

| Type | Purpose | Example |
|------|---------|---------|
| A | Name to IPv4 | `api.example.com. 300 IN A 203.0.113.10` |
| AAAA | Name to IPv6 | `api.example.com. 300 IN AAAA 2001:db8::10` |
| CNAME | Alias to another name | `www.example.com. 300 IN CNAME example.com.` |
| ALIAS / ANAME / Route 53 Alias | Provider-specific CNAME-like at zone apex | `example.com -> my-alb-123.us-east-1.elb.amazonaws.com` |
| MX | Mail servers, with priority | `example.com. 3600 IN MX 10 mail.example.com.` |
| TXT | Arbitrary text: SPF, DKIM, DMARC, domain verification | `"v=spf1 include:_spf.google.com ~all"` |
| NS | Authoritative name servers for a zone | `example.com. 172800 IN NS ns-1.awsdns-01.org.` |
| SOA | Start of authority: primary NS, serial, timers, negative TTL | |
| PTR | Reverse lookup: IP to name | `10.113.0.203.in-addr.arpa. PTR api.example.com.` |
| SRV | Service location: priority, weight, port, target | `_sip._tcp.example.com. SRV 10 60 5060 sip1.example.com.` |
| CAA | Which CAs may issue certificates | `example.com. CAA 0 issue "letsencrypt.org"` |
| HTTPS / SVCB | Service binding: ALPN (h2/h3), IP hints, ECH | `example.com. HTTPS 1 . alpn="h3,h2"` |
| DS / DNSKEY / RRSIG | DNSSEC chain of trust | |

### 4.3.1 CNAME rules that bite

- A CNAME cannot coexist with any other record at the same name. Therefore you **cannot put a CNAME at the zone apex** (`example.com`), because the apex must have SOA and NS records. This is why providers invented ALIAS/flattening, and why AWS Route 53 "Alias" records exist.
- Each CNAME hop may require another lookup. Avoid long chains.
- Route 53 Alias records to AWS resources (ALB, CloudFront, S3 website, API Gateway) are free to query and track the target's IPs automatically.

### 4.3.2 The SOA record and negative caching

```text
example.com. 900 IN SOA ns-1.awsdns-01.org. hostmaster.example.com. (
        2026101201 ; serial
        7200       ; refresh
        900        ; retry
        1209600    ; expire
        300 )      ; minimum = negative caching TTL
```

The last field controls how long resolvers cache **NXDOMAIN** (name does not exist). If you query a name *before* creating it, resolvers will cache "does not exist" for that duration, which explains "I created the record but it still doesn't resolve".

## 4.4 TTL and Caching Strategy

TTL (time to live, in seconds) tells resolvers how long they may cache an answer.

| TTL | Trade-off |
|-----|-----------|
| 30–60 s | Fast failover and migrations; more queries, more resolver load, slightly higher latency |
| 300 s | Common default for application endpoints |
| 3600–86400 s | Stable records (MX, NS, verification TXT); fewer queries, slow to change |

**Migration playbook:** lower the TTL to 60 s at least one old-TTL period *before* the change (if the TTL was 86400, lower it a day ahead), make the change, verify, then raise the TTL again.

> **Note:** Not every client honors TTLs. The JVM historically cached DNS forever when a security manager was installed (`networkaddress.cache.ttl`); some HTTP clients and connection pools resolve once and keep connections for hours. After a DNS cutover, old connections keep going to old IPs until they are closed. DNS changes redirect *new* connections only.

## 4.5 Split-Horizon DNS

**Split-horizon** (split-brain) DNS returns different answers for the same name depending on who asks. The typical use: `api.example.com` resolves to a public load balancer from the Internet but to a private IP from inside the VPC or corporate network.

```text
                 query: api.example.com
                         |
         +---------------+----------------+
         | from Internet                  | from inside VPC
         v                                v
  Public hosted zone                Private hosted zone (associated with VPC)
  api -> 203.0.113.10 (ALB)         api -> 10.0.32.15 (internal NLB)
```

In AWS, a Route 53 **private hosted zone** is associated with one or more VPCs; queries from those VPCs to the VPC resolver use it, everyone else gets the public zone.

**Hybrid DNS:** on-prem and cloud usually need to resolve each other's private names. Route 53 Resolver **inbound endpoints** let on-prem DNS forward queries into the VPC; **outbound endpoints** with forwarding rules send queries for `corp.example.com` to on-prem DNS servers.

> **Warning:** With split-horizon, a private zone for `example.com` completely shadows the public zone for clients in that VPC. If a name exists only in the public zone, VPC clients get NXDOMAIN. Either create a private zone for a subdomain only (`internal.example.com`), or duplicate the needed records.

## 4.6 Routing Policies (Route 53 Style)

Managed DNS providers can return different answers based on policy. Route 53's policies are a useful reference set:

| Policy | Behavior | Use case |
|--------|----------|----------|
| Simple | One record set, possibly multiple values returned in random order | Single endpoint |
| Weighted | Return records in proportion to weights (e.g., 90/10) | Canary or blue/green cutover |
| Latency | Return the region with the lowest measured latency to the resolver | Multi-region active-active |
| Failover | Primary while healthy, secondary when its health check fails | Active-passive DR |
| Geolocation | Based on continent/country/state of the requester | Data residency, localized content |
| Geoproximity | Based on distance, adjustable with a bias | Shift traffic between regions gradually |
| Multivalue answer | Up to 8 healthy records, randomly | Simple client-side load distribution with health checks |
| IP-based | Based on the client's source CIDR | Route specific ISPs or networks |

Health checks drive these policies: Route 53 health checkers probe an endpoint (HTTP/HTTPS/TCP) from multiple locations and mark it unhealthy after a threshold.

> **Tip:** DNS-based failover is only as fast as TTL plus health-check detection time plus client caching. Expect 1–5 minutes in practice. For sub-second failover, use anycast or a global load balancer (AWS Global Accelerator, GCP global LB) in front of regions.

> **Note:** The "location" seen by DNS is the *resolver's* location, not the user's, unless the resolver sends **EDNS Client Subnet** (ECS). Users on a corporate VPN or a far-away public resolver may be routed to a suboptimal region.

## 4.7 DNS in Kubernetes

Kubernetes runs **CoreDNS** as a cluster DNS service (usually `kube-dns` Service at the 10th IP of the service CIDR, e.g., `10.96.0.10`).

| Name | Resolves to |
|------|-------------|
| `my-svc.my-ns.svc.cluster.local` | ClusterIP of the Service |
| `my-svc` (from a pod in `my-ns`) | Same, via search domains |
| Headless Service name | A records for each ready pod IP |
| `pod-0.my-svc.my-ns.svc.cluster.local` | Specific StatefulSet pod |
| `_http._tcp.my-svc.my-ns.svc.cluster.local` | SRV record for named port |

A pod's `/etc/resolv.conf`:

```text
search my-ns.svc.cluster.local svc.cluster.local cluster.local ec2.internal
nameserver 10.96.0.10
options ndots:5
```

### 4.7.1 The ndots:5 problem

With `ndots:5`, any name with fewer than 5 dots is first tried with each search domain. A lookup of `api.stripe.com` (2 dots) generates:

```text
api.stripe.com.my-ns.svc.cluster.local   A + AAAA -> NXDOMAIN
api.stripe.com.svc.cluster.local         A + AAAA -> NXDOMAIN
api.stripe.com.cluster.local             A + AAAA -> NXDOMAIN
api.stripe.com.ec2.internal              A + AAAA -> NXDOMAIN
api.stripe.com                           A + AAAA -> answer
```

Up to 10 queries for one external lookup. At scale this overloads CoreDNS and can hit the AWS VPC resolver limit (1,024 packets per second per ENI). Fixes:

- Use FQDNs with a trailing dot in configuration: `api.stripe.com.`
- Lower ndots per pod: `dnsConfig: { options: [ { name: ndots, value: "2" } ] }`
- Run **NodeLocal DNSCache** (a caching agent on every node).
- Scale CoreDNS and enable its `cache` plugin (on by default).

### 4.7.2 Conntrack and UDP DNS races

On older kernels, parallel A and AAAA queries from the same socket could race in conntrack and one would be dropped, causing 5-second timeouts (the resolver's retry interval). Signs: intermittent latency of exactly 5 s on lookups. Mitigations: NodeLocal DNSCache, `options single-request-reopen` (glibc), or newer kernels.

## 4.8 DNSSEC and Encrypted DNS (Brief)

- **DNSSEC** signs records so resolvers can verify authenticity (RRSIG, DNSKEY, DS chain from the root). It protects integrity, not privacy. Misconfigured DNSSEC (expired signatures, broken DS after a provider migration) makes the entire domain fail to resolve for validating resolvers.
- **DoT** (DNS over TLS, port 853) and **DoH** (DNS over HTTPS, port 443) encrypt the stub-to-resolver hop for privacy. Corporate networks sometimes block them to enforce internal resolvers.

## 4.9 Debugging with dig

### 4.9.1 Reading dig output

```bash
dig www.example.com
```

```text
; <<>> DiG 9.18.28 <<>> www.example.com
;; global options: +cmd
;; Got answer:
;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 41022
;; flags: qr rd ra; QUERY: 1, ANSWER: 2, AUTHORITY: 0, ADDITIONAL: 1

;; QUESTION SECTION:
;www.example.com.               IN      A

;; ANSWER SECTION:
www.example.com.        3600    IN      CNAME   example.com.
example.com.            217     IN      A       93.184.215.14

;; Query time: 3 msec
;; SERVER: 127.0.0.53#53(127.0.0.53) (UDP)
;; WHEN: Mon Oct 12 10:02:11 UTC 2026
;; MSG SIZE  rcvd: 74
```

| Field | Meaning |
|-------|---------|
| `status: NOERROR` | Success. `NXDOMAIN` = name does not exist. `SERVFAIL` = resolver failed (upstream down, DNSSEC failure). `REFUSED` = server will not answer you |
| `flags: qr rd ra` | qr = response, rd = recursion desired, ra = recursion available, **aa** = authoritative answer |
| TTL (217) | Remaining cache time; a counting-down value means it came from cache |
| `SERVER` | Which server actually answered |

> **Note:** `NOERROR` with zero answers ("NODATA") means the name exists but has no record of that type, e.g., you asked for AAAA and only A exists. It is different from NXDOMAIN.

### 4.9.2 The dig commands you will actually use

```bash
dig +short api.example.com                  # just the answer
dig api.example.com AAAA                    # specific record type
dig @8.8.8.8 api.example.com                # ask a specific resolver
dig @ns-1.awsdns-01.org api.example.com     # ask the authoritative server directly (bypass caches)
dig +trace api.example.com                  # follow delegation from root yourself
dig example.com NS +short                   # who is authoritative?
dig -x 203.0.113.10                         # reverse lookup (PTR)
dig +tcp api.example.com                    # force TCP (test if TCP 53 is allowed)
dig api.example.com +norecurse @10.0.0.2    # is it in this resolver's cache?
dig +dnssec example.com                     # show DNSSEC records
getent ahosts api.example.com               # what the app sees (hosts file, nsswitch)
```

### 4.9.3 A debugging procedure

```text
1. What does the APP see?       getent ahosts name     (compare with dig)
2. What does my resolver say?   dig name               (status, TTL, server)
3. What does the AUTHORITY say? dig NS zone; dig @auth-ns name
     - Auth correct, resolver wrong -> caching; wait TTL or flush
     - Auth wrong -> fix the record / check you edited the right zone
4. Is delegation correct?       dig +trace name
     - Registrar NS records must match the hosted zone's NS records
5. Split horizon?               Same query from inside vs outside the network
6. Search domains / ndots?      Query the FQDN with trailing dot
```

> **Interview:** "You changed a DNS record but users still hit the old server. Why?" Cached answers within TTL at recursive resolvers, negative caching if the name previously did not exist, OS and browser caches, applications caching resolutions or holding long-lived connections, and possibly editing the wrong zone (private vs public, or a zone the registrar does not delegate to).

## 4.10 Lab: Run Your Own Authoritative and Recursive DNS

This lab uses Docker to run CoreDNS as an authoritative server for a lab zone, then queries it.

**Step 1.** Create a zone file and Corefile:

```bash
mkdir -p ~/dnslab && cd ~/dnslab
cat > db.lab.test <<'EOF'
$ORIGIN lab.test.
$TTL 60
@     IN SOA ns1.lab.test. admin.lab.test. 2026101201 7200 900 1209600 30
@     IN NS  ns1.lab.test.
ns1   IN A   127.0.0.1
web   IN A   10.0.0.10
web   IN A   10.0.0.11
api   IN CNAME web.lab.test.
_http._tcp IN SRV 10 50 8080 web.lab.test.
@     IN TXT "v=spf1 -all"
EOF
cat > Corefile <<'EOF'
lab.test:53 {
    file /etc/coredns/db.lab.test
    log
    errors
}
.:53 {
    forward . 1.1.1.1 8.8.8.8
    cache 30
    log
}
EOF
```

**Step 2.** Run CoreDNS:

```bash
docker run -d --name coredns -p 1053:53/udp -p 1053:53/tcp \
  -v ~/dnslab:/etc/coredns coredns/coredns:1.11.3 -conf /etc/coredns/Corefile
```

**Step 3.** Query your zone. Note the `aa` flag (authoritative):

```bash
dig @127.0.0.1 -p 1053 web.lab.test
dig @127.0.0.1 -p 1053 api.lab.test +short
dig @127.0.0.1 -p 1053 _http._tcp.lab.test SRV +short
```

```text
web.lab.test.
10.0.0.10
10.0.0.11
10 50 8080 web.lab.test.
```

**Step 4.** Observe negative caching. Query a name that does not exist; note `NXDOMAIN` and the SOA in the AUTHORITY section (its last field, 30, is the negative TTL):

```bash
dig @127.0.0.1 -p 1053 new.lab.test
```

**Step 5.** Observe recursive forwarding and caching. Run twice and watch the TTL count down and query time drop:

```bash
dig @127.0.0.1 -p 1053 github.com | grep -E 'IN\s+A|Query time'
sleep 5; dig @127.0.0.1 -p 1053 github.com | grep -E 'IN\s+A|Query time'
```

**Step 6.** Trace the public delegation path for a real domain:

```bash
dig +trace +nodnssec github.com | grep -E 'NS|A\s'
```

**Step 7.** See the ndots effect. Run a second CoreDNS instance on a user-defined Docker network with a fixed IP, then query it from a client container configured like a Kubernetes pod:

```bash
docker network create --subnet 172.30.0.0/24 dnsnet
docker run -d --name coredns2 --network dnsnet --ip 172.30.0.53 \
  -v ~/dnslab:/etc/coredns coredns/coredns:1.11.3 -conf /etc/coredns/Corefile
docker run --rm --network dnsnet --dns 172.30.0.53 \
  --dns-search a.svc.cluster.local --dns-search svc.cluster.local \
  --dns-opt ndots:5 debian:12-slim getent ahosts github.com
docker logs coredns2 | grep github
```

```text
[INFO] 172.30.0.2:41871 - 1203 "A IN github.com.a.svc.cluster.local. udp 48 false 512" NXDOMAIN qr,rd,ra 123 0.0213s
[INFO] 172.30.0.2:41871 - 5530 "AAAA IN github.com.a.svc.cluster.local. udp 48 false 512" NXDOMAIN qr,rd,ra 123 0.0198s
[INFO] 172.30.0.2:52210 - 7712 "A IN github.com.svc.cluster.local. udp 46 false 512" NXDOMAIN qr,rd,ra 121 0.0201s
...
[INFO] 172.30.0.2:39101 - 1820 "A IN github.com. udp 28 false 512" NOERROR qr,rd,ra 54 0.0102s
```

Repeat with `github.com.` (trailing dot) and count how many queries disappear.

**Step 8.** Clean up: `docker rm -f coredns coredns2 && docker network rm dnsnet`.

## Key takeaways

- DNS is hierarchical, distributed and cached: stub -> recursive resolver -> root -> TLD -> authoritative.
- Applications resolve via `getaddrinfo()` (hosts file, nsswitch, search domains); `dig` bypasses that. Use `getent ahosts` to see what apps see.
- Know your record types; CNAMEs cannot live at the apex (use ALIAS/Alias records) or coexist with other records.
- TTLs control how fast changes propagate; lower them before migrations. NXDOMAIN is cached too (SOA minimum).
- Split-horizon returns different answers inside vs outside; private zones shadow public ones.
- Weighted, latency, failover, and geo policies make DNS a traffic-management tool, but failover is bounded by TTL and client behavior.
- In Kubernetes, `ndots:5` multiplies queries for external names; use FQDNs, tune ndots, and run NodeLocal DNSCache.
- Debug systematically: app view, resolver view, authoritative view, delegation, split-horizon.
