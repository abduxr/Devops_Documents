# Part 5 — HTTP, TLS, Certificates, and Proxies

Almost every service you run speaks HTTP, and almost all of it is wrapped in TLS. This Part explains how HTTP evolved and why each version exists, how TLS establishes trust, how to inspect and debug certificates with `openssl` and `curl`, and how forward and reverse proxies sit in the request path.

## 5.1 HTTP Fundamentals

### 5.1.1 Anatomy of a request and response

HTTP is a request/response protocol. HTTP/1.1 is plain text, which makes it easy to read:

```text
GET /api/v1/users/42 HTTP/1.1
Host: api.example.com
User-Agent: curl/8.9.1
Accept: application/json
Authorization: Bearer eyJhbGciOi...
Connection: keep-alive

HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 61
Cache-Control: no-store
Date: Mon, 12 Oct 2026 10:15:02 GMT

{"id":42,"name":"Ada","email":"ada@example.com","active":true}
```

The `Host` header is mandatory in HTTP/1.1. It is what allows **virtual hosting**: many sites on one IP address, with the server (or load balancer) routing by hostname.

### 5.1.2 Methods and status codes

| Method | Safe | Idempotent | Typical use |
|--------|------|------------|-------------|
| GET | Yes | Yes | Read |
| HEAD | Yes | Yes | Headers only (health checks, size checks) |
| POST | No | No | Create, actions |
| PUT | No | Yes | Replace |
| PATCH | No | No (usually) | Partial update |
| DELETE | No | Yes | Delete |
| OPTIONS | Yes | Yes | CORS preflight, capabilities |

Idempotency matters to infrastructure: proxies and service meshes can safely **retry** idempotent requests on failure, but retrying a POST can create duplicate orders.

| Code | Meaning | Who usually generates it in DevOps context |
|------|---------|-------------------------------------------|
| 200/201/204 | Success | Application |
| 301/302/307/308 | Redirect (308/307 preserve the method) | App or LB (HTTP to HTTPS) |
| 304 | Not modified (cache validation) | App or CDN |
| 400 | Bad request | App, or proxy rejecting malformed headers |
| 401 / 403 | Unauthenticated / forbidden | App, API gateway, WAF |
| 404 | Not found | App, or proxy with no matching route |
| 413 | Payload too large | Proxy (`client_max_body_size` in Nginx) |
| 429 | Too many requests | Rate limiter / API gateway |
| 499 | Client closed request (Nginx-specific) | Client gave up before response |
| 500 | Internal server error | Application bug |
| 502 | Bad gateway: proxy got an invalid or no response from upstream | Proxy/LB (upstream crashed, reset, refused) |
| 503 | Service unavailable | LB with no healthy targets, or app overloaded |
| 504 | Gateway timeout: upstream did not answer in time | Proxy/LB timeout |

> **Tip:** 502, 503 and 504 are almost always produced by the **proxy in front of** your app, not the app itself. They tell you which hop to look at: a 502 means the proxy could talk to something but got garbage, an RST, or a refused connection; a 504 means it waited and gave up. Always check the proxy's logs for the upstream error.

### 5.1.3 Headers that matter to infrastructure

| Header | Purpose |
|--------|---------|
| `Host` | Virtual host routing |
| `X-Forwarded-For` | Original client IP chain, appended by each proxy |
| `X-Forwarded-Proto` | Original scheme (`https`) when TLS terminated at the proxy |
| `Forwarded` | Standardized version of the above (RFC 7239) |
| `X-Request-ID` / `traceparent` | Correlate logs and traces across hops |
| `Connection`, `Keep-Alive` | Connection reuse (HTTP/1.1) |
| `Cache-Control`, `ETag`, `Vary` | Caching behavior at browsers and CDNs |
| `Strict-Transport-Security` | HSTS: force HTTPS for a period |
| `Upgrade` | Switch protocols, e.g., WebSocket |

> **Warning:** `X-Forwarded-For` is client-controlled input. Only trust the entries added by your own proxies (for Nginx use `set_real_ip_from` plus `real_ip_header`; for apps, configure trusted proxy counts). Otherwise anyone can spoof their IP for rate limiting or audit logs.

## 5.2 HTTP Versions

### 5.2.1 HTTP/1.1

- Text protocol, one request at a time per connection.
- **Keep-alive** reuses the TCP connection for multiple requests (default in 1.1).
- **Head-of-line (HOL) blocking**: a slow response blocks the next on that connection. Browsers open about 6 parallel connections per host to work around it.

### 5.2.2 HTTP/2

- **Binary framing**; each request/response is a **stream** on one TCP connection.
- **Multiplexing**: many concurrent streams, no application-level HOL blocking.
- **HPACK** header compression (big win for repetitive headers like cookies and auth tokens).
- Negotiated via TLS **ALPN** (`h2`). Cleartext HTTP/2 (`h2c`) is used internally, e.g., by gRPC.
- **gRPC runs on HTTP/2**, which has load-balancing consequences: one long-lived connection carries all requests, so an L4 load balancer sends everything from one client to one backend. You need L7 (per-request) balancing for gRPC (Part 6, Part 8).
- Still suffers **TCP-level HOL blocking**: one lost packet stalls all streams until it is retransmitted.

### 5.2.3 HTTP/3 and QUIC

- Runs over **QUIC**, a transport built on **UDP port 443**.
- QUIC integrates TLS 1.3 into the transport: connection setup is 1 RTT, and 0-RTT on resumption.
- Streams are independent at the transport layer: a lost packet only stalls its own stream.
- **Connection migration**: connections are identified by a connection ID, not the 5-tuple, so a phone moving from Wi-Fi to cellular keeps its connection.
- Discovered via the `Alt-Svc` response header or the DNS `HTTPS` record. Clients fall back to HTTP/2 if UDP 443 is blocked, which corporate firewalls often do.

```text
              HTTP/1.1          HTTP/2            HTTP/3
Semantics     HTTP              HTTP              HTTP
Framing       text              binary frames     binary frames
Multiplexing  no                streams           streams
Security      TLS (optional)    TLS (in practice) TLS 1.3 built in
Transport     TCP               TCP               QUIC over UDP
New conn RTT  1 (TCP) + TLS     1 (TCP) + TLS     1 (0 on resume)
```

```bash
curl -sI --http2 https://www.cloudflare.com | head -1
curl -sI --http3 https://www.cloudflare.com | head -1   # needs curl built with HTTP/3
curl -sI https://www.google.com | grep -i alt-svc
```

```text
HTTP/2 200
HTTP/3 200
alt-svc: h3=":443"; ma=2592000,h3-29=":443"; ma=2592000
```

### 5.2.4 WebSockets

A WebSocket starts as an HTTP/1.1 request with `Upgrade: websocket` and then becomes a long-lived bidirectional TCP stream. Proxies must be configured to pass the `Upgrade` and `Connection` headers, and idle timeouts must be long enough (or the app must send pings), otherwise connections drop every 60 seconds.

## 5.3 Using curl as a Network Debugger

`curl -v` shows each layer of a request: DNS, TCP, TLS, HTTP.

```bash
curl -v https://api.example.com/health
```

```text
* Host api.example.com:443 was resolved.
* IPv6: (none)
* IPv4: 203.0.113.10
*   Trying 203.0.113.10:443...
* Connected to api.example.com (203.0.113.10) port 443
* ALPN: curl offers h2,http/1.1
* TLSv1.3 (OUT), TLS handshake, Client hello (1):
* TLSv1.3 (IN), TLS handshake, Server hello (2):
* TLSv1.3 (IN), TLS handshake, Certificate (11):
* TLSv1.3 (IN), TLS handshake, CERT verify (15):
* TLSv1.3 (IN), TLS handshake, Finished (20):
* SSL connection using TLSv1.3 / TLS_AES_128_GCM_SHA256 / X25519 / RSASSA-PSS
* ALPN: server accepted h2
* Server certificate:
*  subject: CN=api.example.com
*  start date: Sep  1 00:00:00 2026 GMT
*  expire date: Nov 30 23:59:59 2026 GMT
*  subjectAltName: host "api.example.com" matched cert's "api.example.com"
*  issuer: C=US; O=Let's Encrypt; CN=R11
*  SSL certificate verify ok.
> GET /health HTTP/2
> Host: api.example.com
< HTTP/2 200
< content-type: application/json
{"status":"ok"}
```

Timing breakdown, which tells you *where* the time goes:

```bash
curl -o /dev/null -s -w 'dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}\n' https://api.example.com/
```

```text
dns=0.012 connect=0.045 tls=0.112 ttfb=0.890 total=0.905
```

Interpretation: DNS 12 ms, TCP 33 ms (RTT), TLS 67 ms, then 778 ms waiting for the server. The network is fine; the backend is slow.

Other essential flags:

```bash
curl --resolve api.example.com:443:10.0.32.15 https://api.example.com/   # bypass DNS, keep SNI/Host
curl -H 'Host: api.example.com' http://10.0.32.15/                       # test a vhost by IP (HTTP)
curl --connect-to api.example.com:443:new-lb.example.net:443 https://api.example.com/
curl -k https://...      # skip cert verification (diagnosis only, never in production code)
curl --cacert ca.pem --cert client.pem --key client.key https://...      # mTLS
```

## 5.4 TLS

### 5.4.1 What TLS provides

1. **Confidentiality**: traffic is encrypted.
2. **Integrity**: tampering is detected.
3. **Authentication**: the server proves its identity with a certificate (and optionally the client, in mTLS).

Use TLS 1.2 and 1.3 only. SSLv3, TLS 1.0 and 1.1 are deprecated and insecure.

### 5.4.2 TLS 1.3 handshake

```text
Client                                                    Server
  |--- ClientHello ----------------------------------------->|
  |    supported versions, cipher suites, key_share (X25519), |
  |    SNI = api.example.com, ALPN = [h2, http/1.1]           |
  |                                                           |
  |<-- ServerHello (chosen cipher, key_share) ----------------|
  |    {EncryptedExtensions (ALPN = h2)}                       |
  |    {Certificate chain}                                     |
  |    {CertificateVerify: signature proving key ownership}    |
  |    {Finished}                                              |
  |                                                           |
  |--- {Finished} ------------------------------------------->|
  |--- {HTTP request} (application data, 1 RTT total) ------->|
       { } = encrypted with keys derived from the ECDHE exchange
```

- **Key exchange**: ephemeral Diffie-Hellman (ECDHE). Both sides derive the same session keys without ever sending them. Because keys are ephemeral, stealing the server's private key later cannot decrypt recorded traffic (**forward secrecy**).
- **Authentication**: the certificate binds a public key to a name; `CertificateVerify` proves the server holds the matching private key.
- **TLS 1.2** needs 2 RTTs and allows weaker options (RSA key exchange without forward secrecy). TLS 1.3 removed them.
- **SNI** (Server Name Indication) is sent in the ClientHello so the server knows which certificate to present. Without SNI, a server with many certificates returns the default one, the cause of many "wrong certificate" errors when testing with `openssl` without `-servername`. SNI is visible to observers (ECH, Encrypted Client Hello, addresses this).
- **ALPN** negotiates the application protocol (h2, http/1.1) inside the handshake.

### 5.4.3 Certificates and the chain of trust

An X.509 certificate contains a subject, a public key, a validity period, **Subject Alternative Names** (SANs; the hostnames it is valid for), key usage, and the issuer's signature.

```text
  [Root CA]                    self-signed, in the OS/browser trust store
      | signs
  [Intermediate CA]            sent by the server
      | signs
  [Leaf: api.example.com]      sent by the server

Server must send: leaf + intermediate(s). Not the root.
Client verifies:  each signature up the chain, ends at a trusted root,
                  validity dates, hostname matches a SAN, not revoked.
```

> **Warning:** The most common TLS failure in production is a **missing intermediate**. Browsers often still work (they cache intermediates or fetch them via AIA), but `curl`, Java, Python, and Go clients fail with "unable to get local issuer certificate". Always deploy the **full chain** (`fullchain.pem` from Let's Encrypt).

Hostname matching uses SANs only; the Common Name is ignored by modern clients. A wildcard `*.example.com` matches `api.example.com` but **not** `example.com` or `a.b.example.com`.

### 5.4.4 Certificate lifetimes and automation

Publicly trusted certificate lifetimes are shrinking: CA/Browser Forum rules reduce the maximum from 398 days to 200 days (from March 2026), 100 days (2027), and 47 days (2029). Let's Encrypt issues 90-day certificates. The lesson: **automate issuance and renewal**. Manual certificate management is now an outage generator.

**Let's Encrypt and ACME:** the ACME protocol proves you control a domain with a challenge:

| Challenge | How | When to use |
|-----------|-----|-------------|
| HTTP-01 | Serve a token at `http://domain/.well-known/acme-challenge/<token>` on port 80 | Simple public web servers |
| DNS-01 | Create a TXT record `_acme-challenge.domain` | Wildcards, internal servers, no inbound port 80 |
| TLS-ALPN-01 | Special certificate on port 443 | When only 443 is open |

Tools: `certbot`, `acme.sh`, Caddy (automatic), and in Kubernetes **cert-manager**, which watches `Certificate` resources or Ingress/Gateway annotations and stores certificates in Secrets.

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: platform@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - dns01:
          route53:
            region: us-east-1
```

Use the Let's Encrypt **staging** endpoint while testing to avoid rate limits.

### 5.4.5 mTLS

In **mutual TLS**, the server also requests a certificate from the client and verifies it against a trusted CA. It authenticates *machines/workloads* rather than users. Uses: service-to-service authentication in a service mesh (Part 8), securing APIs between companies, IoT devices, Kubernetes components (kubelet to API server).

```text
Client                                  Server
  |--- ClientHello ----------------------->|
  |<-- ServerHello, Certificate,           |
  |    CertificateRequest (acceptable CAs) |
  |--- Certificate (client cert),          |
  |    CertificateVerify, Finished ------->|  server validates client chain
```

### 5.4.6 openssl commands you need

```bash
# Inspect a live server's certificate chain (always pass -servername for SNI)
openssl s_client -connect api.example.com:443 -servername api.example.com -showcerts </dev/null

# Just the leaf's subject, issuer, dates, SANs
openssl s_client -connect api.example.com:443 -servername api.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -ext subjectAltName

# Days until expiry check (exit code non-zero if expiring within 14 days)
openssl s_client -connect api.example.com:443 -servername api.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -checkend $((14*86400))

# Inspect a certificate file
openssl x509 -in cert.pem -noout -text

# Verify a chain locally
openssl verify -CAfile root.pem -untrusted intermediate.pem leaf.pem

# Does this key match this certificate? (hashes must be identical)
openssl x509 -noout -pubkey -in cert.pem | openssl sha256
openssl pkey -pubout -in key.pem | openssl sha256

# Generate a key and CSR with SANs
openssl req -new -newkey rsa:2048 -nodes -keyout api.key -out api.csr \
  -subj "/CN=api.example.com" -addext "subjectAltName=DNS:api.example.com,DNS:www.example.com"

# Test specific protocol versions
openssl s_client -connect api.example.com:443 -servername api.example.com -tls1_2 </dev/null
```

```text
subject=CN=api.example.com
issuer=C=US, O=Let's Encrypt, CN=R11
notBefore=Sep  1 00:00:00 2026 GMT
notAfter=Nov 30 23:59:59 2026 GMT
X509v3 Subject Alternative Name:
    DNS:api.example.com, DNS:www.example.com
```

### 5.4.7 Common TLS errors

| Error | Likely cause |
|-------|-------------|
| `certificate has expired` | Renewal failed; check automation |
| `unable to get local issuer certificate` | Missing intermediate, or private CA not in client trust store |
| `hostname mismatch` / `no alternative certificate subject name matches` | Wrong cert served (missing SNI, wrong vhost) or name not in SANs |
| `self-signed certificate in certificate chain` | Corporate TLS-inspecting proxy, or private CA |
| `wrong version number` | Speaking TLS to a plain HTTP port (or vice versa) |
| `handshake failure` / `no shared cipher` | Protocol/cipher mismatch, or mTLS client cert missing |

## 5.5 Proxies

### 5.5.1 Forward proxy vs reverse proxy

```text
FORWARD PROXY (acts for clients)          REVERSE PROXY (acts for servers)

 [Client]--+                                              +--[App 1]
 [Client]--+--> [Proxy] ---> Internet      Internet --> [Proxy]--+--[App 2]
 [Client]--+                                              +--[App 3]

 Clients know about it                    Clients think the proxy IS the server
 Egress control, caching, auditing        TLS termination, LB, routing, caching, WAF
 e.g., Squid, corporate proxy             e.g., Nginx, HAProxy, Envoy, ALB, Traefik
```

Forward proxies show up in DevOps as egress control: build agents or servers in a locked-down network reach package repositories only through a proxy. Tools honor `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` environment variables (inconsistently: some only read lowercase names, and `NO_PROXY` CIDR support varies by tool).

For HTTPS through a forward proxy, the client sends `CONNECT api.example.com:443` and the proxy opens a TCP tunnel; it cannot see inside unless it performs **TLS interception** (presenting its own certificates signed by a corporate CA that must be installed on clients, the source of many "self-signed certificate in chain" errors inside corporate networks and Docker builds).

### 5.5.2 Transparent proxies

A transparent proxy intercepts traffic without client configuration, typically with iptables redirection (`REDIRECT`/`TPROXY`). Service mesh sidecars use exactly this to capture all pod traffic.

### 5.5.3 Preserving client identity

A reverse proxy opens a **new** TCP connection to the backend, so the backend sees the proxy's IP. Ways to preserve the client IP:

| Method | Layer | How |
|--------|-------|-----|
| `X-Forwarded-For` / `Forwarded` | L7 | Proxy adds header; app reads it |
| PROXY protocol (v1 text, v2 binary) | L4 | Proxy prepends a header with original addresses before the TCP payload; backend must be configured to expect it |
| Transparent / DSR | L3/L4 | Preserve source IP at network level (NLB with client IP preservation, `externalTrafficPolicy: Local` in Kubernetes) |

> **Warning:** If you enable PROXY protocol on the load balancer but not on the backend (or vice versa), every request fails with 400 errors or broken TLS handshakes, because one side sees unexpected bytes at the start of the stream.

## 5.6 Lab: Build a Private CA, Serve TLS and mTLS with Nginx

**Step 1.** Create a root CA:

```bash
mkdir -p ~/tlslab && cd ~/tlslab
openssl req -x509 -newkey rsa:4096 -nodes -keyout ca.key -out ca.pem -days 3650 \
  -subj "/CN=Lab Root CA"
```

**Step 2.** Create a server key and CSR, then sign it with the CA, including SANs:

```bash
openssl req -new -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=app.lab.test"
cat > server.ext <<'EOF'
subjectAltName = DNS:app.lab.test, DNS:localhost, IP:127.0.0.1
extendedKeyUsage = serverAuth
EOF
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.pem -days 30 -extfile server.ext
openssl x509 -in server.pem -noout -subject -issuer -ext subjectAltName
```

**Step 3.** Create a client certificate for mTLS:

```bash
openssl req -new -newkey rsa:2048 -nodes -keyout client.key -out client.csr -subj "/CN=billing-service"
echo "extendedKeyUsage = clientAuth" > client.ext
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out client.pem -days 30 -extfile client.ext
```

**Step 4.** Write an Nginx config with TLS and optional client verification:

```nginx
# ~/tlslab/default.conf
server {
    listen 443 ssl;
    http2 on;
    server_name app.lab.test;

    ssl_certificate     /etc/nginx/tls/server.pem;
    ssl_certificate_key /etc/nginx/tls/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    ssl_client_certificate /etc/nginx/tls/ca.pem;
    ssl_verify_client optional;

    location /public {
        return 200 "hello, anyone\n";
    }
    location /private {
        if ($ssl_client_verify != SUCCESS) { return 403 "client cert required\n"; }
        return 200 "hello, $ssl_client_s_dn\n";
    }
}
```

**Step 5.** Run it:

```bash
docker run -d --name tlslab -p 8443:443 \
  -v ~/tlslab:/etc/nginx/tls:ro -v ~/tlslab/default.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:1.27
```

**Step 6.** Test, observing each failure mode:

```bash
curl https://localhost:8443/public                       # fails: CA not trusted
curl --cacert ca.pem https://localhost:8443/public       # works
curl --cacert ca.pem https://127.0.0.1:8443/public       # works: IP is in SANs
curl --cacert ca.pem https://localhost:8443/private      # 403: no client cert
curl --cacert ca.pem --cert client.pem --key client.key https://localhost:8443/private
```

```text
curl: (60) SSL certificate problem: unable to get local issuer certificate
hello, anyone
hello, anyone
client cert required
hello, CN=billing-service
```

**Step 7.** Inspect the handshake and negotiated protocol:

```bash
openssl s_client -connect localhost:8443 -servername app.lab.test -CAfile ca.pem -alpn h2 </dev/null 2>/dev/null \
  | grep -E 'Verify return|Protocol|ALPN'
```

**Step 8.** Clean up: `docker rm -f tlslab`.

## Key takeaways

- HTTP/1.1 is text with keep-alive; HTTP/2 multiplexes streams over one TCP connection (and gRPC rides on it); HTTP/3 runs over QUIC/UDP 443 and removes TCP head-of-line blocking.
- 502/503/504 come from proxies; they point to the hop between proxy and upstream.
- `curl -v` and `curl -w` timing break a request into DNS, TCP, TLS, and server time.
- TLS 1.3 completes in 1 RTT with ephemeral ECDHE (forward secrecy); SNI selects the certificate; ALPN selects the protocol.
- Servers must send the full chain (leaf + intermediates); hostname validation uses SANs.
- Certificate lifetimes are shrinking toward 47 days: automate with ACME (certbot, cert-manager).
- mTLS authenticates both sides and underpins zero-trust service-to-service security.
- Reverse proxies terminate connections; preserve client IPs with X-Forwarded-For or PROXY protocol and only trust headers from your own proxies.
