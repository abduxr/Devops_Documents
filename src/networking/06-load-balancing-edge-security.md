# Part 6 — Load Balancers, API Gateways, CDNs, Firewalls, and VPNs

This Part covers the infrastructure that sits between users and your services: load balancers that spread traffic and hide failures, API gateways that enforce policy, CDNs that move content to the edge, firewalls that decide what is allowed, and VPNs that connect private networks securely.

## 6.1 Load Balancing Concepts

A load balancer distributes requests across a pool of backends (targets, upstreams) so you get **scalability** (add more backends), **availability** (route around failures), and **operability** (drain a node for deployment without dropping traffic).

### 6.1.1 L4 vs L7

```text
L4 (transport) load balancer                L7 (application) load balancer

Client --TCP--> [LB] --TCP--> Backend       Client --TLS/HTTP--> [LB] --HTTP--> Backend
       sees: IPs, ports, TCP flags                 sees: Host, path, headers, cookies, methods
       decides per CONNECTION                      decides per REQUEST
       (may pass through TLS untouched)            (terminates TLS, new upstream connection)
```

| | L4 | L7 |
|---|----|----|
| Routing basis | 5-tuple | Host, path, headers, cookies, gRPC method |
| TLS | Passthrough (or terminate in some products) | Terminates (and may re-encrypt) |
| Performance | Very high, low latency, millions of connections | Higher CPU per request |
| Protocols | Any TCP/UDP (databases, MQTT, DNS, game servers) | HTTP/1.1, HTTP/2, gRPC, WebSocket |
| Client IP | Often preserved | Via `X-Forwarded-For` |
| Features | Simple health checks, connection balancing | Retries, rewrites, redirects, auth, rate limits, WAF, canary by header |
| AWS | Network Load Balancer (NLB) | Application Load Balancer (ALB) |
| Software | HAProxy (mode tcp), Nginx `stream`, IPVS, Maglev, Cilium | Nginx, HAProxy (mode http), Envoy, Traefik |

> **Interview:** "When would you use an NLB instead of an ALB?" Non-HTTP protocols, static IP addresses (NLB supports Elastic IPs per AZ, useful for customer allowlists), extreme throughput or very low latency, preserving client source IP natively, TLS passthrough so the backend holds the private key, and as a PrivateLink endpoint service (which requires an NLB or Gateway Load Balancer).

### 6.1.2 Balancing algorithms

| Algorithm | How | Good for | Watch out |
|-----------|-----|----------|-----------|
| Round robin | Next backend in order | Homogeneous, short requests | Ignores load |
| Weighted round robin | Proportional to weight | Mixed instance sizes, canaries | Static |
| Least connections | Fewest active connections | Long or variable requests | Needs accurate state |
| Least outstanding requests | Fewest in-flight requests (ALB option) | Variable latency APIs | |
| Random with two choices (P2C) | Pick 2 at random, take the less loaded | Large fleets, distributed LBs (Envoy default for least request) | |
| IP hash / source hash | Hash of client IP | Simple stickiness | Uneven behind NAT |
| Consistent hashing (ring, Maglev) | Hash of a key; minimal remapping when pool changes | Caches, sharded backends | Hot keys |

### 6.1.3 Health checks

- **Active**: the LB probes each backend (`GET /healthz` every 5–10 s). After N consecutive failures (unhealthy threshold) it stops routing; after M successes it resumes.
- **Passive** (outlier detection): the LB watches real traffic and ejects a backend that returns too many errors or resets.

Designing a good health endpoint:

- **Liveness-style** checks ("process is up and can serve") belong on the load balancer.
- Do **not** make the LB health check depend on downstream dependencies (database, other services). If the DB blips, every backend fails its check simultaneously and the LB has nothing to route to: you turned a partial outage into a total one. Many LBs "fail open" when all targets are unhealthy precisely because of this.
- Keep it cheap and fast; it is called constantly.

### 6.1.4 Sticky sessions

**Session affinity** sends a client to the same backend each time, via an LB-generated cookie, an application cookie, or source IP hash. It is needed when session state lives in backend memory. Downsides: uneven load, lost sessions when a backend dies, and slower draining. Prefer stateless backends with shared session storage (Redis) and use stickiness only when you must.

### 6.1.5 Connection draining and graceful deployments

When a backend is deregistered, the LB stops sending new requests but lets in-flight requests finish for the **deregistration delay** (ALB default 300 s). Your app must also handle `SIGTERM` gracefully: stop accepting, finish in-flight work, then exit. In Kubernetes, add a short `preStop` sleep so the endpoint is removed from load balancers before the process stops (Part 8).

### 6.1.6 Timeouts: the source of mysterious 502s and 504s

Every hop has timeouts, and they must be **ordered**: each upstream's keep-alive idle timeout must be **longer** than the downstream proxy's.

```text
Client --> ALB (idle 60 s) --> Nginx (keepalive_timeout 75 s) --> App (keep-alive 90 s)
```

If the app closes an idle keep-alive connection at 5 s (Node.js `server.keepAliveTimeout` default is 5 s) while the ALB thinks it is reusable for 60 s, the ALB occasionally sends a request on a connection the app just closed and gets a reset: an **intermittent 502**. Fix by setting the app's keep-alive timeout above the LB idle timeout.

## 6.2 Nginx and HAProxy Configurations

### 6.2.1 Nginx as an L7 load balancer

```nginx
upstream api_backend {
    least_conn;
    server 10.0.1.11:8080 max_fails=3 fail_timeout=10s;
    server 10.0.1.12:8080 max_fails=3 fail_timeout=10s;
    server 10.0.1.13:8080 weight=2;
    server 10.0.1.14:8080 backup;
    keepalive 64;                       # pool of idle upstream connections
}

server {
    listen 443 ssl;
    http2 on;
    server_name api.example.com;
    ssl_certificate     /etc/ssl/api/fullchain.pem;
    ssl_certificate_key /etc/ssl/api/privkey.pem;

    client_max_body_size 20m;

    location / {
        proxy_pass http://api_backend;
        proxy_http_version 1.1;          # required for upstream keepalive
        proxy_set_header Connection "";  # do not forward "close"
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 3s;
        proxy_read_timeout 30s;
        proxy_next_upstream error timeout http_502 http_503;
        proxy_next_upstream_tries 2;
    }

    location /ws/ {
        proxy_pass http://api_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1h;
    }
}

server {
    listen 80;
    server_name api.example.com;
    return 308 https://$host$request_uri;
}
```

Open-source Nginx performs **passive** health checks only (`max_fails`/`fail_timeout`); active checks require Nginx Plus or a module. Note `proxy_next_upstream` retries non-idempotent requests only if you add `non_idempotent`, which is correct default behavior.

### 6.2.2 Nginx as an L4 load balancer

```nginx
stream {
    upstream postgres_replicas {
        server 10.0.64.10:5432;
        server 10.0.65.10:5432;
    }
    server {
        listen 5432;
        proxy_pass postgres_replicas;
        proxy_connect_timeout 2s;
        proxy_timeout 10m;
    }
}
```

### 6.2.3 HAProxy

```ini
global
    log stdout format raw local0
    maxconn 50000

defaults
    mode http
    log global
    option httplog
    option forwardfor
    timeout connect 3s
    timeout client  60s
    timeout server  30s
    timeout http-keep-alive 10s

frontend fe_https
    bind :443 ssl crt /etc/haproxy/certs/api.pem alpn h2,http/1.1
    http-request set-header X-Forwarded-Proto https
    acl is_admin path_beg /admin
    http-request deny if is_admin !{ src 10.0.0.0/8 }
    use_backend be_admin if is_admin
    default_backend be_api

backend be_api
    balance leastconn
    option httpchk
    http-check send meth GET uri /healthz ver HTTP/1.1 hdr Host api.example.com
    http-check expect status 200
    cookie SRV insert indirect nocache
    server api1 10.0.1.11:8080 check inter 5s fall 3 rise 2 cookie api1
    server api2 10.0.1.12:8080 check inter 5s fall 3 rise 2 cookie api2

backend be_admin
    server admin1 10.0.1.20:8080 check

frontend fe_stats
    bind 127.0.0.1:8404
    stats enable
    stats uri /stats
```

HAProxy is notable for its detailed logs: the **termination state** code in each log line (e.g., `sH--`, `SC--`, `cD--`) tells you which side ended the session and why, which is invaluable when chasing 502s and 504s.

## 6.3 Reverse Proxies and API Gateways

An **API gateway** is a reverse proxy specialized for APIs, adding cross-cutting concerns so each service does not reimplement them:

| Concern | Example |
|---------|---------|
| Authentication | Validate JWTs, OAuth2 introspection, API keys, mTLS |
| Authorization | Scopes, per-route policies |
| Rate limiting and quotas | 100 req/s per API key, burst 200 |
| Request/response transformation | Header injection, path rewrites, protocol translation (REST to gRPC) |
| Routing and versioning | `/v1/*` to old service, `/v2/*` to new |
| Observability | Uniform access logs, metrics, tracing headers |
| Developer portal and keys | Usage plans |

Examples: AWS API Gateway, Kong, Apigee, Tyk, Envoy Gateway, and Kubernetes Gateway API implementations. The difference between an "L7 load balancer", "reverse proxy", "ingress controller", and "API gateway" is mostly feature set; underneath they are frequently the same software (Envoy and Nginx power many of them).

> **Tip:** Rate limiting at the gateway needs shared state across gateway replicas (Redis or a rate-limit service), or each replica enforces its own limit and the effective limit becomes limit x replicas.

## 6.4 CDNs

A **CDN** (Content Delivery Network) is a globally distributed set of reverse proxies (points of presence, PoPs) that cache content close to users and shield your origin.

```text
User (Tokyo) --DNS/anycast--> Tokyo PoP --(cache HIT)--> response in 10 ms
                                 |
                                 +--(cache MISS)--> [Regional/shield cache] --> Origin (us-east-1)
```

Benefits: lower latency (TCP and TLS terminate near the user, so the handshake RTTs are short), offloaded origin bandwidth, DDoS absorption, and edge features (WAF, bot management, edge compute).

Caching is controlled by headers:

```text
Cache-Control: public, max-age=31536000, immutable   # fingerprinted static assets (app.3f9a1c.js)
Cache-Control: public, s-maxage=60, stale-while-revalidate=30   # CDN caches 60 s, browsers revalidate
Cache-Control: private, no-store                      # never cache (user-specific data)
Vary: Accept-Encoding                                 # separate cache entries per encoding
```

- The **cache key** is usually host + path + selected query strings/headers. Including too much (all cookies) destroys hit ratio; including too little leaks one user's content to another.
- **Invalidation** is slow and sometimes costly; prefer versioned filenames for assets.
- Restrict the origin to accept traffic only from the CDN (AWS CloudFront managed prefix list in security groups, origin access control for S3, or a secret header), otherwise attackers bypass the CDN and WAF.

## 6.5 Firewalls

### 6.5.1 Stateless vs stateful

- **Stateless** (packet filter): each packet is evaluated alone. To allow a client to reach a server on 443, you must also allow return traffic to the client's **ephemeral port range** (1024–65535).
- **Stateful**: the firewall tracks connections (conntrack). If outbound SYN was allowed, the replies are automatically allowed. Nearly all modern firewalls are stateful.
- **Next-generation / L7 firewalls**: inspect application protocols, TLS SNI, URLs, and identity.

### 6.5.2 Linux: netfilter, iptables, nftables

Netfilter hooks in the Linux kernel process every packet. `iptables` is the classic frontend; `nftables` is its modern replacement (most distros now run `iptables-nft`, which translates iptables commands to nftables).

```text
                         Incoming packet
                               |
                         [PREROUTING]  (nat: DNAT, raw, mangle)
                               |
                     routing decision: for me?
                     /                     \
                  yes                       no
                   |                         |
               [INPUT]                   [FORWARD]   (containers, routing)
                   |                         |
            local process                    |
                   |                         |
               [OUTPUT]                      |
                    \                       /
                         [POSTROUTING]  (nat: SNAT / MASQUERADE)
                               |
                         Outgoing packet
```

A minimal host firewall with nftables:

```bash
cat > /etc/nftables.conf <<'EOF'
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif lo accept
    ip protocol icmp accept
    ip6 nexthdr icmpv6 accept
    tcp dport 22 ip saddr 10.0.0.0/8 accept
    tcp dport { 80, 443 } accept
    counter log prefix "nft-drop: " drop
  }
}
EOF
nft -f /etc/nftables.conf
nft list ruleset
```

The equivalent iptables approach:

```bash
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -s 10.0.0.0/8 -j ACCEPT
iptables -A INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT
iptables -P INPUT DROP
iptables -L -n -v --line-numbers     # counters show which rules match
```

> **Warning:** Set the established/related rule and your SSH allow rule **before** changing the default policy to DROP, or you will lock yourself out of a remote machine. On cloud instances, prefer security groups for perimeter control and keep host firewalls simple.

### 6.5.3 Security groups vs network ACLs (AWS)

| | Security Group | Network ACL |
|---|---------------|-------------|
| Attached to | ENI (instance, pod ENI, LB, RDS) | Subnet |
| State | **Stateful**: return traffic automatically allowed | **Stateless**: return traffic needs explicit rules |
| Rules | Allow only | Allow and deny |
| Evaluation | All rules evaluated together | Numbered, lowest first, first match wins |
| Source can be | CIDR, prefix list, **another security group** | CIDR only |
| Default | New SG: deny all inbound, allow all outbound | Default NACL: allow all |
| Typical use | Primary workload firewall | Coarse subnet guardrails, explicit denies (block an IP range) |

Security-group-to-security-group references are powerful: "allow 5432 from `sg-app`" means any ENI with `sg-app` attached, regardless of IP, which survives autoscaling.

NACL ephemeral port trap:

```text
Inbound to web subnet:   rule 100 allow TCP 443 from 0.0.0.0/0
Outbound from subnet:    rule 100 allow TCP 1024-65535 to 0.0.0.0/0   <- replies to clients' ephemeral ports
And for instances calling out (e.g., to an API on 443):
Outbound:                rule 110 allow TCP 443 to 0.0.0.0/0
Inbound:                 rule 110 allow TCP 1024-65535 from 0.0.0.0/0  <- replies coming back
```

Forget the ephemeral rules and connections hang (timeouts, not refusals), which is why NACL mistakes are so hard to spot.

### 6.5.4 Web Application Firewalls (preview)

A WAF inspects HTTP requests for attacks (SQL injection, XSS, known bad bots) and enforces rate limits. It sits at the CDN or L7 load balancer. Part 9 covers WAF and DDoS in more detail.

## 6.6 VPNs

A VPN builds an encrypted tunnel across an untrusted network so that two private networks (site-to-site) or a user and a network (remote access) can communicate as if directly connected.

### 6.6.1 IPsec

IPsec is the standard for site-to-site VPNs and cloud VPN gateways.

- **IKE** (Internet Key Exchange, UDP 500, and UDP 4500 with NAT traversal) authenticates peers (pre-shared key or certificates) and negotiates keys. IKEv2 is the current version.
- **Phase 1** builds the IKE SA (a secure control channel). **Phase 2** builds the IPsec SAs (child SAs) that carry data.
- **ESP** (IP protocol 50) encrypts the payload. **AH** (protocol 51) authenticates only and is rarely used.
- **Tunnel mode** encrypts the whole original packet and adds a new IP header (site-to-site). **Transport mode** encrypts only the payload.
- **Policy-based** VPNs define "interesting traffic" by selectors; **route-based** VPNs create a tunnel interface and use routing (static or BGP). Cloud VPNs are route-based with BGP for dynamic failover.

```text
 On-prem 192.168.0.0/16                                   AWS VPC 10.0.0.0/16
 [Router/FW] ==== IPsec tunnel 1 (BGP) ==== [AWS VPN endpoint A] --+
      \======== IPsec tunnel 2 (BGP) ====== [AWS VPN endpoint B] --+-- VGW / Transit Gateway
 Each AWS Site-to-Site VPN connection = 2 tunnels for redundancy, ~1.25 Gbit/s per tunnel
```

Common IPsec problems: mismatched proposals (encryption/hash/DH group), mismatched traffic selectors, UDP 500/4500 or ESP blocked, overlapping CIDRs, and MTU (IPsec overhead of 50–80 bytes; clamp TCP MSS to around 1360–1379).

### 6.6.2 WireGuard

WireGuard is a modern, minimal VPN built into the Linux kernel. It uses a fixed, modern crypto suite (Curve25519, ChaCha20-Poly1305), runs over a single UDP port (51820 by convention), and identifies peers by public key. Tailscale and many mesh VPNs are built on it.

```ini
# /etc/wireguard/wg0.conf on server (public IP 203.0.113.50)
[Interface]
Address = 10.200.0.1/24
ListenPort = 51820
PrivateKey = <server-private-key>

[Peer]
# laptop
PublicKey = <laptop-public-key>
AllowedIPs = 10.200.0.2/32
```

```ini
# /etc/wireguard/wg0.conf on laptop
[Interface]
Address = 10.200.0.2/24
PrivateKey = <laptop-private-key>
DNS = 10.0.0.2

[Peer]
PublicKey = <server-public-key>
Endpoint = 203.0.113.50:51820
AllowedIPs = 10.200.0.0/24, 10.0.0.0/16   # routes sent into the tunnel
PersistentKeepalive = 25                  # keep NAT mappings alive
```

```bash
wg genkey | tee privatekey | wg pubkey > publickey
sudo wg-quick up wg0
sudo wg show
```

```text
interface: wg0
  public key: kV3...=
  listening port: 51820

peer: 9Hq...=
  endpoint: 198.51.100.7:41200
  allowed ips: 10.200.0.2/32
  latest handshake: 12 seconds ago
  transfer: 18.42 MiB received, 2.10 MiB sent
```

`AllowedIPs` does double duty: on send it acts as a routing table (which peer gets this destination), on receive it acts as an ACL (which source IPs are accepted from this peer). If "latest handshake" never appears, the UDP port is blocked or keys are wrong; WireGuard is silent by design and will not reply to unauthenticated packets.

| | IPsec (IKEv2) | WireGuard | OpenVPN |
|---|---------------|-----------|---------|
| Transport | UDP 500/4500, ESP | UDP (one port) | UDP or TCP (often 1194/443) |
| Complexity | High, many options | Very low | Medium |
| Performance | High (hardware offload) | Very high | Lower (user space) |
| Interop | Universal (routers, clouds) | Linux/BSD/mobile, few hardware appliances | Wide client support |
| Typical use | Site-to-site, cloud VPN | Remote access, mesh, server-to-server | Legacy remote access |

## 6.7 Lab: Load Balancing with HAProxy and Failure Injection

**Step 1.** Create a Docker network and three backends that identify themselves:

```bash
docker network create lbnet
for i in 1 2 3; do
  docker run -d --name web$i --network lbnet hashicorp/http-echo:0.2.3 -listen=:8080 -text="backend web$i"
done
```

**Step 2.** Write an HAProxy config:

```bash
mkdir -p ~/lblab && cat > ~/lblab/haproxy.cfg <<'EOF'
global
    log stdout format raw local0
defaults
    mode http
    log global
    option httplog
    timeout connect 2s
    timeout client 30s
    timeout server 30s
frontend fe
    bind :80
    default_backend be
backend be
    balance roundrobin
    option httpchk GET /
    server web1 web1:8080 check inter 2s fall 2 rise 2
    server web2 web2:8080 check inter 2s fall 2 rise 2
    server web3 web3:8080 check inter 2s fall 2 rise 2
frontend stats
    bind :8404
    stats enable
    stats uri /
EOF
docker run -d --name lb --network lbnet -p 8080:80 -p 8404:8404 \
  -v ~/lblab/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro haproxy:3.0
```

**Step 3.** Observe round robin:

```bash
for i in $(seq 6); do curl -s localhost:8080; done
```

```text
backend web1
backend web2
backend web3
backend web1
backend web2
backend web3
```

**Step 4.** Kill a backend and watch the health check remove it (about 4 s with `inter 2s fall 2`):

```bash
docker stop web2
docker logs lb 2>&1 | grep -i 'web2'
for i in $(seq 4); do curl -s localhost:8080; done
```

```text
Server be/web2 is DOWN, reason: Layer4 timeout, check duration: 2001ms. 2 active and 0 backup servers left.
backend web1
backend web3
backend web1
backend web3
```

Open `http://localhost:8404/` to see the stats page.

**Step 5.** Change `balance roundrobin` to `balance source`, restart the LB (`docker restart lb`), and note that all your requests now land on the same backend (source-IP stickiness).

**Step 6.** Bring web2 back with `docker start web2` and watch it return after two successful checks (`rise 2`).

**Step 7.** Clean up: `docker rm -f lb web1 web2 web3 && docker network rm lbnet`.

## Key takeaways

- L4 balancers route connections by 5-tuple (fast, protocol-agnostic); L7 balancers route requests by HTTP attributes and terminate TLS.
- Choose algorithms deliberately: least-connections or P2C for variable workloads, consistent hashing for caches.
- Health checks should reflect the instance's own ability to serve, not its dependencies; drain connections during deploys.
- Order timeouts so upstream keep-alive timeouts exceed downstream idle timeouts, or you will get intermittent 502s.
- API gateways add auth, rate limiting, and transformation on top of reverse proxying; CDNs cache at the edge and are controlled by Cache-Control and cache keys.
- Stateful firewalls track connections; stateless ones (NACLs) need explicit ephemeral-port return rules. Security groups are stateful, allow-only, and can reference other groups.
- IPsec (IKE UDP 500/4500, ESP) is the site-to-site standard; WireGuard is a simple, fast UDP VPN where AllowedIPs is both route table and ACL.
