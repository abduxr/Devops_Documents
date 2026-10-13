# Part 8 — Container Networking, Kubernetes Networking, and Service Mesh

Container networking is not new networking; it is the namespaces, veth pairs, bridges, routes, and NAT rules from Parts 1–3, automated. Once you see that, Kubernetes networking stops being magic. This Part goes from a single Docker host to Kubernetes Services, Ingress and Gateway API, NetworkPolicy, CoreDNS, and service meshes.

## 8.1 Docker Networking

### 8.1.1 The default bridge

When Docker starts, it creates a Linux bridge `docker0` (default `172.17.0.1/16`). For each container it:

1. Creates a network namespace for the container.
2. Creates a **veth pair**; one end becomes `eth0` inside the container, the other is attached to `docker0`.
3. Assigns an IP from the bridge subnet and sets the container's default route to `172.17.0.1`.
4. Adds iptables rules for NAT.

```text
                        Host network namespace
  +------------------------------------------------------------------+
  |   eth0 10.0.1.25 (to the world)                                  |
  |      ^                                                           |
  |      |  iptables POSTROUTING: MASQUERADE 172.17.0.0/16 -> eth0  |
  |      |  iptables PREROUTING:  DNAT :8080 -> 172.17.0.2:80        |
  |   docker0 172.17.0.1/16 (bridge)                                 |
  |      |                    |                                      |
  |   vethA1b2              vethC3d4                                 |
  +------|--------------------|--------------------------------------+
         |                    |
  +------|-------+     +------|-------+
  | eth0         |     | eth0         |
  | 172.17.0.2   |     | 172.17.0.3   |
  | container A  |     | container B  |
  +--------------+     +--------------+
```

### 8.1.2 Seeing it on the host

```bash
docker run -d --name web -p 8080:80 nginx:1.27
ip link show master docker0
docker inspect -f '{{.NetworkSettings.IPAddress}}' web
sudo iptables -t nat -S | grep -E 'MASQUERADE|DNAT'
```

```text
7: veth4f1c2a9@if6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue master docker0 state UP
172.17.0.2
-A POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE
-A DOCKER ! -i docker0 -p tcp -m tcp --dport 8080 -j DNAT --to-destination 172.17.0.2:80
```

The `@if6` suffix means the veth's peer is interface index 6 in another namespace (the container's `eth0`). Read the two rules:

- **MASQUERADE**: traffic from containers leaving via anything other than docker0 gets the host's IP as source (outbound PAT).
- **DNAT**: traffic arriving on host port 8080 has its destination rewritten to the container. This is all `-p 8080:80` does.

> **Warning:** Docker's published ports are inserted into the `nat` table and the `DOCKER` chain, before your `INPUT` rules are consulted. A host firewall like UFW that only filters `INPUT` does **not** block published container ports. Use the `DOCKER-USER` chain for filtering, or publish on a specific address: `-p 127.0.0.1:8080:80`.

### 8.1.3 Network drivers

| Driver | Behavior | Use |
|--------|----------|-----|
| `bridge` (default) | Private bridge, NAT to outside | Single-host apps |
| User-defined bridge | Same, plus **embedded DNS** (127.0.0.11) for container-name resolution and isolation between networks | Compose projects (the default there) |
| `host` | No network namespace; shares host stack | Max performance, network tools; port conflicts possible |
| `none` | Loopback only | Isolation |
| `macvlan` / `ipvlan` | Container gets an address on the physical LAN | Legacy apps expecting L2 presence |
| `overlay` | VXLAN across hosts (Swarm) | Multi-host |

On the default bridge, containers cannot resolve each other by name; on a user-defined network they can. That is why `docker compose` services find each other by service name.

### 8.1.4 Common Docker networking bugs

| Symptom | Cause |
|---------|-------|
| `curl localhost:8080` from host fails, but container is running | App inside listens on `127.0.0.1` instead of `0.0.0.0` |
| Container cannot reach a corporate network | Docker subnet overlaps with VPN/VPC CIDR; change `default-address-pools` |
| Container cannot reach the Internet | `net.ipv4.ip_forward=0`, or FORWARD chain policy/rules blocking |
| Large downloads hang inside containers on a VPN or overlay | MTU mismatch; set Docker's `mtu` option or clamp MSS |
| `host.docker.internal` does not resolve on Linux | Add `--add-host=host.docker.internal:host-gateway` |

## 8.2 The Kubernetes Network Model

Kubernetes defines requirements, not an implementation:

1. Every **pod gets its own IP** address.
2. Pods can communicate with all other pods on any node **without NAT**.
3. Agents on a node (kubelet) can communicate with all pods on that node.
4. Containers in a pod share one network namespace (they reach each other via `localhost`).

There are four distinct communication problems, solved by different components:

| Problem | Solved by |
|---------|-----------|
| Container to container in a pod | Shared network namespace (the `pause` container holds it) |
| Pod to pod | **CNI plugin** |
| Pod to Service | **kube-proxy** (or eBPF replacement) and CoreDNS |
| External to Service | NodePort / LoadBalancer / Ingress / Gateway |

Three separate CIDRs are configured in every cluster and must not overlap with each other or with networks the cluster must reach:

```text
Node network      10.0.0.0/16     (VPC subnets; node IPs)
Pod network       10.244.0.0/16   (per-node slices, e.g., node1 10.244.1.0/24)
Service network   10.96.0.0/12    (virtual IPs; exist only in kube-proxy rules)
```

On AWS EKS with the VPC CNI, pods get IPs **from the VPC subnets** directly, so pod CIDR = node subnets. That is simple and fast but consumes VPC addresses quickly; mitigate with prefix delegation, secondary CIDRs in `100.64.0.0/10` (custom networking), or IPv6.

## 8.3 CNI Plugins

**CNI** (Container Network Interface) is a spec: when a pod is created, the container runtime calls a CNI plugin binary with `ADD`, and the plugin creates interfaces, assigns an IP (via IPAM), and sets routes. On `DEL`, it cleans up. Configuration lives in `/etc/cni/net.d/` and binaries in `/opt/cni/bin/`.

### 8.3.1 How pod-to-pod across nodes works

Two general approaches:

```text
1) Overlay (encapsulation): VXLAN / IP-in-IP / Geneve

 Node1 10.0.1.10                                  Node2 10.0.2.20
 [pod A 10.244.1.5]                               [pod B 10.244.2.7]
       |                                                 ^
   inner: 10.244.1.5 -> 10.244.2.7                       |
   outer: 10.0.1.10 -> 10.0.2.20 UDP 4789 =========> decapsulate
 Works on any underlay; costs MTU (50 bytes for VXLAN) and some CPU.

2) Native routing (no encapsulation)

 Node routes: 10.244.2.0/24 via 10.0.2.20 (installed by BGP or the cloud route table)
 Packets travel unmodified. Faster, but the underlay must know pod routes,
 or pod IPs must be native underlay IPs (AWS VPC CNI, Azure CNI, GKE VPC-native).
```

### 8.3.2 Popular CNIs

| CNI | Data plane | Highlights |
|-----|-----------|------------|
| Flannel | VXLAN (or host-gw) | Simple, no NetworkPolicy by itself |
| Calico | iptables/nftables or eBPF; BGP native routing, IP-in-IP or VXLAN | Mature NetworkPolicy, BGP peering with physical network, global policies |
| Cilium | eBPF | kube-proxy replacement, L3–L7 policy, Hubble flow observability, transparent encryption (WireGuard/IPsec), BGP, Cluster Mesh, Gateway API |
| AWS VPC CNI | Native VPC IPs on ENIs | Pods are first-class VPC citizens; security groups for pods |
| Azure CNI (Overlay / Powered by Cilium) | VNet or overlay | Azure native |
| GKE Dataplane V2 | Cilium-based eBPF | Default on GKE |

> **Note:** **eBPF** lets programs run safely inside the kernel at hooks such as the network driver (XDP) and the traffic control layer. Cilium uses it to implement Service load balancing, policy, and observability without long iptables chains, which matters at scale: iptables rules are evaluated linearly, and clusters with thousands of Services have tens of thousands of rules.

## 8.4 Services

Pods are ephemeral; their IPs change. A **Service** provides a stable virtual IP and DNS name in front of a set of pods selected by labels. The control plane tracks ready pod IPs in **EndpointSlices**.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: shop
spec:
  type: ClusterIP
  selector:
    app: web
  ports:
    - name: http
      port: 80          # Service port
      targetPort: 8080  # container port
```

### 8.4.1 Service types

| Type | Reachable from | How |
|------|---------------|-----|
| ClusterIP (default) | Inside the cluster | Virtual IP from the service CIDR |
| NodePort | Any node IP on a port in 30000–32767 | ClusterIP + a port opened on every node |
| LoadBalancer | External, via a cloud or MetalLB LB | NodePort + ClusterIP + provisioned LB (or direct pod-IP targets with AWS LB Controller in IP mode) |
| ExternalName | Inside the cluster | DNS CNAME to an external name, no proxying |
| Headless (`clusterIP: None`) | Inside the cluster | DNS returns pod IPs directly; used by StatefulSets and client-side load balancing (gRPC) |

### 8.4.2 kube-proxy: how a ClusterIP actually works

The ClusterIP is not assigned to any interface. It exists only as rules on every node. kube-proxy watches Services and EndpointSlices and programs the data plane.

```text
Pod A (10.244.1.5) connects to web.shop (10.96.120.15:80)
   |
   v  netfilter on Pod A's node (OUTPUT/PREROUTING, nat table)
KUBE-SERVICES: dst 10.96.120.15:80 -> KUBE-SVC-WEB
KUBE-SVC-WEB:  random 1/3 -> KUBE-SEP-1 ; random 1/2 -> KUBE-SEP-2 ; else KUBE-SEP-3
KUBE-SEP-2:    DNAT to 10.244.2.7:8080
   |
   v  routed via CNI to node 2, conntrack remembers the mapping so replies are un-DNATed
Pod B (10.244.2.7:8080)
```

```bash
sudo iptables -t nat -S KUBE-SERVICES | grep 'shop/web'
sudo iptables -t nat -S KUBE-SVC-ABCDEF1234567890
```

```text
-A KUBE-SERVICES -d 10.96.120.15/32 -p tcp -m comment --comment "shop/web:http cluster IP" -m tcp --dport 80 -j KUBE-SVC-ABCDEF1234567890
-A KUBE-SVC-ABCDEF1234567890 -m comment --comment "shop/web:http -> 10.244.1.9:8080" -m statistic --mode random --probability 0.33333333349 -j KUBE-SEP-AAAA
-A KUBE-SVC-ABCDEF1234567890 -m comment --comment "shop/web:http -> 10.244.2.7:8080" -m statistic --mode random --probability 0.50000000000 -j KUBE-SEP-BBBB
-A KUBE-SVC-ABCDEF1234567890 -m comment --comment "shop/web:http -> 10.244.3.4:8080" -j KUBE-SEP-CCCC
```

| kube-proxy mode | Notes |
|-----------------|-------|
| iptables | Default for years; linear rule chains; random backend selection per connection |
| IPVS | Kernel L4 load balancer with hash tables, more algorithms (rr, lc, sh) |
| nftables | Newer default-candidate backend (GA in Kubernetes 1.33), more efficient updates |
| None (eBPF replacement) | Cilium or Calico eBPF handle Services directly |

> **Interview:** "Why can't I ping a ClusterIP?" Because the ClusterIP has no interface and kube-proxy only installs rules for the Service's TCP/UDP ports. ICMP to the VIP matches nothing (with IPVS mode, the VIP is bound to a dummy interface so ping may answer, which confuses people further). Test Services with `curl` or `nc` on the port, never with ping.

Important consequence: **balancing is per connection**. HTTP/2 and gRPC clients keep one long-lived connection, so all their requests go to one pod. Use a headless Service with client-side balancing, or a mesh/L7 proxy that balances per request.

### 8.4.3 externalTrafficPolicy and source IPs

With the default `externalTrafficPolicy: Cluster`, a packet arriving at any node's NodePort may be forwarded to a pod on another node, and is SNATed so the reply returns through the same node: the pod sees the node IP, not the client. With `externalTrafficPolicy: Local`, nodes only send to local pods (no extra hop, **client IP preserved**), and nodes without a ready pod fail the LB health check so the LB avoids them.

## 8.5 Ingress and Gateway API

### 8.5.1 Ingress

An **Ingress** resource declares L7 HTTP routing rules (host, path, TLS); an **Ingress controller** (a reverse proxy such as Nginx, Traefik, HAProxy, or a cloud LB controller) implements them.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: shop
  namespace: shop
spec:
  ingressClassName: alb
  tls:
    - hosts: [shop.example.com]
      secretName: shop-tls
  rules:
    - host: shop.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend: { service: { name: api, port: { number: 80 } } }
          - path: /
            pathType: Prefix
            backend: { service: { name: web, port: { number: 80 } } }
```

Ingress's limitations (HTTP only, features hidden in controller-specific annotations, one resource mixing infrastructure and app concerns) led to Gateway API. The widely used community `ingress-nginx` controller was retired in 2026, which makes this a good time to standardize on Gateway API for new work.

### 8.5.2 Gateway API

Gateway API (GA since v1.0) splits responsibilities by role:

```text
GatewayClass   (infrastructure provider)   "which implementation": Envoy Gateway, Cilium, Istio, AWS, GKE...
     |
Gateway        (platform/cluster operator) listeners: ports, protocols, hostnames, TLS certs
     |
HTTPRoute / GRPCRoute / TLSRoute / TCPRoute   (application teams) routing rules, attached to a Gateway
```

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: public
  namespace: infra
spec:
  gatewayClassName: envoy
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      hostname: "*.example.com"
      tls:
        certificateRefs: [{ name: wildcard-example-com }]
      allowedRoutes:
        namespaces: { from: All }
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: shop
  namespace: shop
spec:
  parentRefs: [{ name: public, namespace: infra }]
  hostnames: ["shop.example.com"]
  rules:
    - matches: [{ path: { type: PathPrefix, value: /api } }]
      backendRefs:
        - { name: api-v1, port: 80, weight: 90 }
        - { name: api-v2, port: 80, weight: 10 }   # canary, portable across implementations
    - backendRefs: [{ name: web, port: 80 }]
```

Traffic splitting, header matching, redirects, and header modification are part of the standard API rather than annotations.

## 8.6 NetworkPolicy

By default, **all pods can talk to all pods** in every namespace. A **NetworkPolicy** selects pods and restricts their ingress and/or egress. It is enforced by the CNI (Flannel alone does nothing). Policies are additive allow-lists: once any policy selects a pod for a direction, everything not explicitly allowed in that direction is denied.

```yaml
# 1) Default deny all ingress and egress in the namespace
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny, namespace: shop }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
# 2) Allow DNS egress for every pod (otherwise nothing resolves)
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-dns, namespace: shop }
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
          podSelector: { matchLabels: { k8s-app: kube-dns } }
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
---
# 3) web may receive from the ingress namespace; api may receive from web; api may reach the DB CIDR
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: api, namespace: shop }
spec:
  podSelector: { matchLabels: { app: api } }
  policyTypes: [Ingress, Egress]
  ingress:
    - from: [{ podSelector: { matchLabels: { app: web } } }]
      ports: [{ protocol: TCP, port: 8080 }]
  egress:
    - to: [{ ipBlock: { cidr: 10.0.64.0/22 } }]
      ports: [{ protocol: TCP, port: 5432 }]
```

> **Warning:** In a single `from` entry, `namespaceSelector` and `podSelector` in the **same** list item are ANDed (pods with that label in those namespaces). As **separate** list items (each starting with `-`) they are ORed (any pod in those namespaces, or matching pods in this namespace). One dash changes the security meaning. Also note that NetworkPolicy matches pod IPs *after* Service DNAT, so policies refer to pod labels and ports, not Service ports.

Standard NetworkPolicy is L3/L4 and namespace-scoped. Cilium (`CiliumNetworkPolicy`, including DNS/FQDN and HTTP-aware rules) and Calico (`GlobalNetworkPolicy`) extend it; the upstream AdminNetworkPolicy API adds cluster-wide policies.

## 8.7 CoreDNS

CoreDNS runs as a Deployment in `kube-system`, behind the `kube-dns` Service. Its configuration is a Corefile in a ConfigMap:

```text
.:53 {
    errors
    health { lameduck 5s }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
        ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf { max_concurrent 1000 }
    cache 30
    loop
    reload
    loadbalance
}
corp.example.com:53 {
    forward . 10.50.0.53 10.50.0.54
}
```

Cluster names are answered by the `kubernetes` plugin; everything else is forwarded to the node's resolver (on EKS, the VPC resolver). The second block is a **stub domain**: on-prem names go to on-prem DNS. See Part 4 for `ndots:5` and NodeLocal DNSCache.

Debugging DNS from inside the cluster:

```bash
kubectl run -it --rm dnsdebug --image=nicolaka/netshoot --restart=Never -- bash
# inside:
cat /etc/resolv.conf
dig web.shop.svc.cluster.local +short
dig +search web +short
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=50
```

## 8.8 Pod Lifecycle and Networking Gotchas

- **Readiness probes** control whether a pod's IP is in EndpointSlices. A failing readiness probe removes the pod from Services; a failing liveness probe restarts it. Never make liveness depend on downstream services.
- **Graceful shutdown race**: when a pod is deleted, endpoint removal (propagated to kube-proxy on every node and to external LBs) happens in parallel with `SIGTERM`. If the app exits immediately, some nodes still send it traffic and clients see connection refused or 502s. Add `preStop: { exec: { command: ["sleep", "10"] } }` (or the native `sleep` action) and handle SIGTERM by draining.
- **Hairpin traffic**: a pod calling its own Service may be load-balanced back to itself; the CNI must support hairpin mode.
- **MTU**: overlay CNIs reduce pod MTU; mismatches cause hangs on large responses (Part 9).
- **Conntrack table exhaustion**: busy nodes can fill `nf_conntrack_max`, producing `nf_conntrack: table full, dropping packet` in `dmesg`.

## 8.9 Service Mesh

A **service mesh** moves service-to-service networking concerns out of application code into infrastructure: mTLS, retries, timeouts, circuit breaking, traffic splitting, and uniform telemetry.

### 8.9.1 Architecture

```text
Sidecar model (Istio classic, Linkerd)

  +----------- Pod A -----------+                +----------- Pod B -----------+
  | [app] -> localhost -> [proxy]| ==== mTLS ===>|[proxy] -> localhost -> [app]|
  +-----------------------------+                +-----------------------------+
          ^ iptables redirect captures all traffic into the sidecar
                       \                          /
                        +---- Control plane -----+
                          (istiod / linkerd-destination): config, service discovery,
                          certificate authority issuing workload identities

Ambient / sidecarless model (Istio ambient, Cilium)
  Per-node L4 proxy (ztunnel) handles mTLS and L4 policy;
  optional per-namespace L7 "waypoint" proxies only where L7 features are needed.
```

- **Data plane**: proxies (Envoy for Istio; a Rust micro-proxy for Linkerd) that handle the actual traffic.
- **Control plane**: distributes routing config, endpoints, and issues short-lived certificates.
- **Identity**: each workload gets a certificate encoding its identity (SPIFFE ID, e.g., `spiffe://cluster.local/ns/shop/sa/api`), derived from its Kubernetes ServiceAccount. Authorization policies use identities, not IPs.

### 8.9.2 Example: Istio traffic management and security

```yaml
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata: { name: default, namespace: shop }
spec:
  mtls: { mode: STRICT }          # reject plaintext between workloads
---
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata: { name: api-allow-web, namespace: shop }
spec:
  selector: { matchLabels: { app: api } }
  action: ALLOW
  rules:
    - from: [{ source: { principals: ["cluster.local/ns/shop/sa/web"] } }]
      to: [{ operation: { methods: ["GET", "POST"], paths: ["/v1/*"] } }]
---
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata: { name: api, namespace: shop }
spec:
  hosts: [api]
  http:
    - timeout: 3s
      retries: { attempts: 2, perTryTimeout: 1s, retryOn: "5xx,reset,connect-failure" }
      route:
        - { destination: { host: api, subset: v1 }, weight: 95 }
        - { destination: { host: api, subset: v2 }, weight: 5 }
```

### 8.9.3 Istio vs Linkerd, and when not to use a mesh

| | Istio | Linkerd |
|---|-------|---------|
| Proxy | Envoy (sidecar) or ztunnel + waypoints (ambient) | linkerd2-proxy (Rust, sidecar) |
| Scope | Very feature-rich: gateways, multi-cluster, extensive traffic policy, VM integration | Focused on simplicity: mTLS, metrics, retries, traffic splitting |
| Operational cost | Higher | Lower |
| Config | VirtualService/DestinationRule plus Gateway API | Gateway API (HTTPRoute) and its own policy CRDs |

Do not adopt a mesh just because it is fashionable. It adds latency (roughly a millisecond or less per hop for sidecars), resource overhead, upgrade complexity, and a new class of failures. Adopt it when you need zero-trust mTLS between many services, consistent L7 telemetry, or advanced traffic management across many teams. Retries configured in both the app and the mesh multiply (2 x 3 = 6 attempts) and can turn a slow dependency into a self-inflicted DDoS.

## 8.10 Lab: Kubernetes Networking Hands-On with kind

You need Docker, `kind`, and `kubectl`. We disable kind's default CNI and install Calico so NetworkPolicy is enforced.

**Step 1.** Create a cluster:

```bash
cat > kind.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  podSubnet: 192.168.0.0/16
  serviceSubnet: 10.96.0.0/12
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
kind create cluster --name netlab --config kind.yaml
kubectl get nodes        # NotReady until a CNI is installed
```

**Step 2.** Install Calico and wait for nodes to become Ready:

```bash
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml
kubectl -n kube-system rollout status ds/calico-node --timeout=180s
kubectl get nodes -o wide
```

**Step 3.** Deploy an app and a Service:

```bash
kubectl create namespace shop
kubectl -n shop create deployment web --image=hashicorp/http-echo:0.2.3 --replicas=3 -- /http-echo -listen=:8080 -text=hello
kubectl -n shop expose deployment web --port=80 --target-port=8080
kubectl -n shop get pods -o wide
kubectl -n shop get svc,endpointslices
```

Note each pod's IP from a different node's slice of 192.168.0.0/16, and the ClusterIP from 10.96.0.0/12.

**Step 4.** Test from a debug pod, by Service name, FQDN, and pod IP:

```bash
kubectl -n shop run client --image=nicolaka/netshoot --restart=Never -- sleep 3600
kubectl -n shop exec client -- curl -s web
kubectl -n shop exec client -- curl -s web.shop.svc.cluster.local
kubectl -n shop exec client -- cat /etc/resolv.conf
kubectl -n shop exec client -- ping -c1 -W1 $(kubectl -n shop get svc web -o jsonpath='{.spec.clusterIP}')   # fails: expected
```

**Step 5.** Look at kube-proxy's rules on a node:

```bash
docker exec netlab-worker iptables -t nat -S KUBE-SERVICES | grep shop/web
```

**Step 6.** Apply default deny, then observe that the client times out (and that DNS also breaks):

```bash
kubectl apply -n shop -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
EOF
kubectl -n shop exec client -- curl -s -m 3 web || echo "blocked"
```

**Step 7.** Allow DNS egress and client-to-web traffic, then retest:

```bash
kubectl apply -n shop -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-dns }
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
          podSelector: { matchLabels: { k8s-app: kube-dns } }
      ports: [{ protocol: UDP, port: 53 }, { protocol: TCP, port: 53 }]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: client-to-web }
spec:
  podSelector: { matchLabels: { run: client } }
  policyTypes: [Egress]
  egress:
    - to: [{ podSelector: { matchLabels: { app: web } } }]
      ports: [{ protocol: TCP, port: 8080 }]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: web-from-client }
spec:
  podSelector: { matchLabels: { app: web } }
  policyTypes: [Ingress]
  ingress:
    - from: [{ podSelector: { matchLabels: { run: client } } }]
      ports: [{ protocol: TCP, port: 8080 }]
EOF
kubectl -n shop exec client -- curl -s -m 3 web
```

Notice the egress rule uses the **pod port 8080**, not the Service port 80, because policy is evaluated after DNAT.

**Step 8.** Clean up: `kind delete cluster --name netlab`.

## Key takeaways

- Docker networking is namespaces + veth + a bridge + iptables MASQUERADE (outbound) and DNAT (published ports); published ports bypass INPUT-chain firewalls.
- Kubernetes requires every pod to have a routable IP without NAT; the CNI implements it with overlays or native routing.
- Keep node, pod, and service CIDRs distinct and non-overlapping with everything the cluster must reach.
- A ClusterIP is a virtual IP implemented by kube-proxy (iptables/IPVS/nftables) or eBPF; it balances per connection and does not answer ping.
- `externalTrafficPolicy: Local` preserves client IPs and avoids extra hops.
- Gateway API is the role-oriented successor to Ingress, with portable traffic splitting and matching.
- NetworkPolicy is default-allow until a policy selects a pod; always allow DNS when you default-deny egress, and mind AND vs OR selector semantics.
- Service meshes provide identity-based mTLS, L7 traffic control, and telemetry, at a real operational cost.
