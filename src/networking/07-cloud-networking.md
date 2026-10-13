# Part 7 — Cloud Networking: AWS VPC in Depth, Azure and GCP, Terraform

Cloud networks look like the networks from Parts 1–6 (subnets, routes, gateways, firewalls), but they are implemented as software in the provider's hypervisors. There is no real broadcast, ARP is answered by the hypervisor, and every "device" is an API object. This Part uses AWS as the deep example because its concepts map cleanly onto the other providers, then shows Azure and GCP equivalents and builds a production-style VPC with Terraform.

## 7.1 AWS VPC Building Blocks

### 7.1.1 VPC

A **VPC** (Virtual Private Cloud) is a logically isolated network in one **region**. You give it a primary IPv4 CIDR between /16 and /28 (from RFC 1918 or other ranges) and can add secondary CIDRs and an IPv6 /56.

```text
Region us-east-1
+---------------------------------------------------------------------------+
| VPC 10.0.0.0/16                                                           |
|                                                                           |
|  AZ us-east-1a                     AZ us-east-1b                          |
|  +-----------------------------+   +-----------------------------+        |
|  | Public  10.0.48.0/24        |   | Public  10.0.49.0/24        |        |
|  |   [ALB node] [NAT GW + EIP] |   |   [ALB node] [NAT GW + EIP] |        |
|  +-----------------------------+   +-----------------------------+        |
|  | Private 10.0.0.0/20         |   | Private 10.0.16.0/20        |        |
|  |   [EC2 / EKS nodes / pods]  |   |   [EC2 / EKS nodes / pods]  |        |
|  +-----------------------------+   +-----------------------------+        |
|  | DB      10.0.64.0/24        |   | DB      10.0.65.0/24        |        |
|  |   [RDS primary]             |   |   [RDS standby]             |        |
|  +-----------------------------+   +-----------------------------+        |
|                                                                           |
|               [Internet Gateway]     [S3 Gateway Endpoint]                |
+---------------------------------------------------------------------------+
```

### 7.1.2 Subnets

- A subnet lives in exactly **one Availability Zone**. High availability means at least one subnet per AZ per tier.
- AWS reserves 5 addresses per subnet (Part 2): in `10.0.48.0/24`, `.0` network, `.1` VPC router, `.2` DNS resolver, `.3` reserved, `.255` last.
- "Public" and "private" are not subnet settings. **A subnet is public if its route table has a route to an Internet Gateway.** That is the whole definition.

### 7.1.3 Route tables

Each subnet is associated with one route table (the **main** route table if you do not choose). Every route table has an unremovable **local** route for the VPC CIDR.

```text
Public route table                       Private route table (AZ-a)
Destination      Target                  Destination      Target
10.0.0.0/16      local                   10.0.0.0/16      local
0.0.0.0/0        igw-0abc                0.0.0.0/0        nat-0aaa (NAT GW in AZ-a)
::/0             igw-0abc                pl-63a5400a (S3) vpce-0s3 (gateway endpoint)
                                         10.50.0.0/16     tgw-0123 (Transit Gateway to on-prem)
```

Longest prefix match applies (Part 3). Per-AZ private route tables let each AZ use its own NAT gateway, avoiding cross-AZ dependency and data charges.

### 7.1.4 Internet Gateway (IGW)

The IGW is a horizontally scaled, highly available VPC attachment that does two things: it is the target for Internet-bound routes, and it performs **1:1 NAT** between an instance's private IP and its public IP/Elastic IP. The instance's OS never sees its public IP (`ip addr` shows only the private address); that mapping lives in the IGW. For Internet access, an instance needs all three: a public IP (or EIP), a route to the IGW, and permissive SG/NACL rules.

### 7.1.5 NAT Gateway

A NAT gateway lets instances in private subnets initiate outbound IPv4 connections while remaining unreachable from the Internet (PAT, Part 2).

- Deploy it in a **public** subnet; point private subnets' `0.0.0.0/0` at it.
- It is AZ-scoped: deploy **one per AZ** for resilience. A single NAT gateway is a single point of failure and incurs cross-AZ data charges.
- It is billed per hour **and per GB processed**. Large pulls through NAT (container images, S3 traffic, package mirrors) can cost more than the compute. Use VPC endpoints for AWS services.
- Idle timeout is 350 s; about 55,000 simultaneous connections per unique destination per public IP (you can add secondary IPs).
- For IPv6, use an **egress-only Internet gateway** instead.

### 7.1.6 Elastic Network Interfaces and Elastic IPs

An **ENI** is a virtual NIC with a primary private IP, optional secondary IPs or prefixes, a MAC, security groups, and a source/destination check flag. EKS's VPC CNI attaches ENIs and secondary IPs (or /28 prefixes with prefix delegation) to nodes and gives them to pods. To run a NAT instance, firewall appliance, or VPN router on EC2, **disable the source/destination check** or the hypervisor drops traffic not addressed to the ENI.

An **Elastic IP** is a static public IPv4 address you own until you release it. All public IPv4 addresses (including auto-assigned ones) are billed hourly.

### 7.1.7 Security Groups and NACLs in the VPC

Part 6 compared them in detail. A typical three-tier design:

| Security group | Inbound | Source |
|----------------|---------|--------|
| `sg-alb` | TCP 443 | 0.0.0.0/0 (or CloudFront prefix list) |
| `sg-app` | TCP 8080 | `sg-alb` |
| `sg-db` | TCP 5432 | `sg-app` |

Keep NACLs at their default allow-all unless you have a specific need (blocking a CIDR, compliance boundaries). Debugging stateless rules across many subnets is expensive.

### 7.1.8 VPC DNS

The **Route 53 Resolver** runs at the VPC base address +2 (`10.0.0.2`) and also at `169.254.169.253`. It resolves public names, private hosted zones associated with the VPC, and AWS internal names (`ip-10-0-1-25.ec2.internal`). Two VPC attributes must be enabled for private DNS and interface endpoints to work: `enableDnsSupport` and `enableDnsHostnames`.

## 7.2 Connecting VPCs and Networks

### 7.2.1 VPC Peering

A peering connection is a private, non-transitive link between two VPCs (same or different account/region).

```text
   VPC A 10.0.0.0/16 <==peering==> VPC B 10.1.0.0/16 <==peering==> VPC C 10.2.0.0/16
   A cannot reach C through B. Peering is NOT transitive.
```

Rules: CIDRs must not overlap; you must add routes in **both** VPCs' route tables (`10.1.0.0/16 -> pcx-...`); security groups must allow the peer's CIDR (or reference peer SGs in the same region); no edge-to-edge routing (A cannot use B's NAT gateway, IGW, or VPN). A full mesh of N VPCs needs N(N-1)/2 peerings, which does not scale beyond a handful.

### 7.2.2 Transit Gateway

A **Transit Gateway** (TGW) is a regional, managed router that VPCs, VPNs, Direct Connect gateways, and other TGWs (via peering) attach to. It replaces a mesh with a hub-and-spoke.

```text
             +--------------------- Transit Gateway ----------------------+
             |  RT "prod":  10.0.0.0/16 -> att-prod   10.50.0.0/16 -> vpn |
             |  RT "shared": all VPC CIDRs propagated                      |
             +-----+--------------+---------------+-------------+--------+
                   |              |               |             |
              VPC prod       VPC staging     VPC shared      Site-to-Site VPN
            10.0.0.0/16     10.10.0.0/16    10.20.0.0/16    on-prem 10.50.0.0/16
```

Concepts:

- **Attachment**: a VPC (via an ENI in a subnet per AZ), VPN, Direct Connect gateway, or peering.
- **Association**: each attachment is associated with one TGW route table, which is used to route traffic *coming from* that attachment.
- **Propagation**: an attachment can automatically install its CIDRs into one or more TGW route tables.
- Segmentation comes from multiple TGW route tables: prod and staging both reach shared services but not each other.
- VPC route tables still need routes pointing at the TGW (`10.0.0.0/8 -> tgw-...`).
- Centralized egress: route all VPCs' `0.0.0.0/0` to an egress VPC with NAT gateways or a firewall (AWS Network Firewall or Gateway Load Balancer appliances).

### 7.2.3 Hybrid connectivity

| Option | What | Notes |
|--------|------|-------|
| Site-to-Site VPN | IPsec over the Internet, 2 tunnels, BGP or static | Quick, cheap, about 1.25 Gbit/s per tunnel, Internet latency |
| Direct Connect | Dedicated private fiber to an AWS location | Consistent latency, 1–100 Gbit/s, weeks to provision; not encrypted by default (use MACsec or VPN over DX) |
| Client VPN | Managed OpenVPN-based remote access | User access |

### 7.2.4 VPC Endpoints and PrivateLink

Endpoints let private resources reach AWS services (or third-party services) **without** an IGW or NAT gateway, keeping traffic on the AWS network.

| | Gateway endpoint | Interface endpoint (PrivateLink) |
|---|------------------|----------------------------------|
| Services | S3 and DynamoDB only | Most AWS services, SaaS, your own services |
| How | Route table entry with a prefix list | ENI with a private IP in your subnets |
| DNS | Normal public names, routed privately | Private DNS overrides the service name to the ENI IPs |
| Security | Endpoint policy | Endpoint policy + security group on the ENI |
| Cost | Free | Hourly per AZ + per GB |
| Reach from on-prem/peered VPC | No | Yes |

**PrivateLink endpoint services** let you expose your own service (behind an NLB or Gateway Load Balancer) to consumers in other VPCs or accounts, one direction only, even with **overlapping CIDRs**, because the consumer only sees an ENI in its own VPC.

```text
Consumer VPC 10.0.0.0/16                         Provider VPC 10.0.0.0/16 (overlap is fine)
 [app] --> [Interface endpoint ENI 10.0.3.41] ===PrivateLink===> [NLB] --> [service]
```

> **Tip:** Add gateway endpoints for S3 and DynamoDB to every VPC on day one. They are free, reduce NAT gateway data charges, and are a common answer to "why is our NAT bill so high?". For EKS in private subnets, interface endpoints for ECR (api and dkr), STS, and CloudWatch Logs let nodes work without any Internet egress.

### 7.2.5 Observability: Flow Logs and Reachability Analyzer

**VPC Flow Logs** record accepted and rejected flows per ENI, subnet, or VPC:

```text
version account-id   interface-id          srcaddr    dstaddr    srcport dstport proto pkts bytes start      end        action log-status
2       123456789012 eni-0a1b2c3d4e5f67890 10.0.1.25  10.0.64.10 51544   5432    6     10   840   1760263200 1760263260 ACCEPT OK
2       123456789012 eni-0a1b2c3d4e5f67890 198.51.100.9 10.0.1.25 44211  22      6     3    180   1760263200 1760263260 REJECT OK
```

`REJECT` means a security group or NACL dropped it. If you see an `ACCEPT` inbound but a `REJECT` for the return flow, suspect a NACL (stateless). Flow logs do not show DNS queries to the Route 53 Resolver (use Resolver query logs), metadata service traffic, or packet contents.

**Reachability Analyzer** statically analyzes the configuration between two resources and tells you which component (route, SG, NACL) blocks a path, without sending packets. Use it before reaching for tcpdump.

## 7.3 Azure and GCP Equivalents

| Concept | AWS | Azure | GCP |
|---------|-----|-------|-----|
| Private network | VPC (regional) | VNet (regional) | VPC (**global**; subnets are regional) |
| Subnet scope | AZ | Region (spans zones) | Region (spans zones) |
| Instance firewall | Security Group (stateful) | NSG (stateful, allow and deny, priorities) on NIC or subnet; ASGs for grouping | VPC firewall rules / policies (stateful), targeted by network tags or service accounts |
| Subnet stateless ACL | NACL | (none; NSG at subnet) | (none) |
| Custom routing | Route tables | User-Defined Routes (UDR) | Routes (with tags), Cloud Router for dynamic |
| Internet egress NAT | NAT Gateway | NAT Gateway | Cloud NAT (software-defined, no appliance) |
| Network peering | VPC Peering | VNet Peering (non-transitive) | VPC Network Peering (non-transitive) |
| Hub router | Transit Gateway | Virtual WAN / hub-spoke with Azure Firewall or NVA | Network Connectivity Center |
| Private service access | PrivateLink / interface endpoints | Private Endpoint / Private Link | Private Service Connect |
| Shared networking across accounts | RAM-shared subnets | (subscriptions + peering) | Shared VPC (host and service projects) |
| Dedicated link | Direct Connect | ExpressRoute | Cloud Interconnect |
| Managed DNS | Route 53 | Azure DNS / Private DNS zones | Cloud DNS |
| L7 LB | ALB | Application Gateway, Front Door (global) | Global external Application LB |
| L4 LB | NLB | Azure Load Balancer | Network LB / passthrough |
| Flow logs | VPC Flow Logs | NSG / VNet flow logs | VPC Flow Logs |

Notable differences:

- **GCP VPCs are global**: one VPC can have subnets in every region, and instances communicate across regions over Google's backbone without peering.
- **Azure reserves 5 IPs** per subnet and requires some services to have dedicated subnets (`GatewaySubnet`, `AzureFirewallSubnet`, delegated subnets).
- **GCP firewall rules** apply to the whole VPC and target instances by tag or service account, rather than being attached to interfaces.

## 7.4 Terraform: A Production-Style VPC

The configuration below builds the design from Section 7.1: three AZs, public/private/database subnets, an IGW, one NAT gateway per AZ, per-AZ private route tables, an S3 gateway endpoint, flow logs, and tiered security groups. It uses `cidrsubnet()` so the plan is derived from one CIDR.

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
}

provider "aws" {
  region = var.region
}

variable "region"   { default = "us-east-1" }
variable "vpc_cidr" { default = "10.0.0.0/16" }
variable "name"     { default = "prod" }

data "aws_availability_zones" "available" { state = "available" }

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
  # /20 private subnets: 10.0.0.0/20, 10.0.16.0/20, 10.0.32.0/20
  private_cidrs = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i)]
  # /24 public subnets:  10.0.48.0/24, 10.0.49.0/24, 10.0.50.0/24
  public_cidrs  = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, 48 + i)]
  # /24 database subnets: 10.0.64.0/24, 10.0.65.0/24, 10.0.66.0/24
  db_cidrs      = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, 64 + i)]
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-igw" }
}

resource "aws_subnet" "public" {
  count                   = 3
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags = { Name = "${var.name}-public-${local.azs[count.index]}", "kubernetes.io/role/elb" = "1" }
}

resource "aws_subnet" "private" {
  count             = 3
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags = { Name = "${var.name}-private-${local.azs[count.index]}", "kubernetes.io/role/internal-elb" = "1" }
}

resource "aws_subnet" "db" {
  count             = 3
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.db_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags = { Name = "${var.name}-db-${local.azs[count.index]}" }
}

# Public routing: default route to the IGW
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 3
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One NAT gateway per AZ
resource "aws_eip" "nat" {
  count  = 3
  domain = "vpc"
  tags   = { Name = "${var.name}-nat-${local.azs[count.index]}" }
}

resource "aws_nat_gateway" "this" {
  count         = 3
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = { Name = "${var.name}-nat-${local.azs[count.index]}" }
  depends_on    = [aws_internet_gateway.this]
}

# Per-AZ private route tables, each using its own AZ's NAT gateway
resource "aws_route_table" "private" {
  count  = 3
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }
  tags = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table_association" "private" {
  count          = 3
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Database subnets: no Internet route at all
resource "aws_route_table" "db" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-db" }
}

resource "aws_route_table_association" "db" {
  count          = 3
  subnet_id      = aws_subnet.db[count.index].id
  route_table_id = aws_route_table.db.id
}

# Free S3 gateway endpoint on private and db route tables
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(aws_route_table.private[*].id, [aws_route_table.db.id])
}

# Tiered security groups
resource "aws_security_group" "alb" {
  name   = "${var.name}-alb"
  vpc_id = aws_vpc.this.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name   = "${var.name}-app"
  vpc_id = aws_vpc.this.id
  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name   = "${var.name}-db"
  vpc_id = aws_vpc.this.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

# Flow logs to CloudWatch (IAM role omitted for brevity; see aws_flow_log docs)
# resource "aws_flow_log" "this" { ... traffic_type = "ALL" ... }

output "vpc_id"             { value = aws_vpc.this.id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "public_subnet_ids"  { value = aws_subnet.public[*].id }
output "subnet_plan" {
  value = { public = local.public_cidrs, private = local.private_cidrs, db = local.db_cidrs }
}
```

> **Note:** For real projects, the community module `terraform-aws-modules/vpc/aws` implements all of this with options for single vs per-AZ NAT, IPv6, endpoints, and flow logs. Writing it by hand once is the best way to understand what that module does.

## 7.5 Lab: Build and Verify the VPC

You need an AWS account, credentials with VPC/EC2 permissions, and Terraform 1.6+. NAT gateways cost money per hour; destroy the lab when finished.

**Step 1.** Verify the subnet math without touching AWS:

```bash
terraform console <<'EOF'
[for i in range(3) : cidrsubnet("10.0.0.0/16", 4, i)]
[for i in range(3) : cidrsubnet("10.0.0.0/16", 8, 48 + i)]
EOF
```

```text
tolist([
  "10.0.0.0/20",
  "10.0.16.0/20",
  "10.0.32.0/20",
])
tolist([
  "10.0.48.0/24",
  "10.0.49.0/24",
  "10.0.50.0/24",
])
```

**Step 2.** Save the configuration as `main.tf`, then:

```bash
terraform init
terraform plan -out tfplan
terraform apply tfplan
```

**Step 3.** Inspect what was created:

```bash
VPC=$(terraform output -raw vpc_id)
aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC \
  --query 'Subnets[].[Tags[?Key==`Name`]|[0].Value,CidrBlock,AvailabilityZone,AvailableIpAddressCount]' --output table
aws ec2 describe-route-tables --filters Name=vpc-id,Values=$VPC \
  --query 'RouteTables[].{name:Tags[?Key==`Name`]|[0].Value,routes:Routes[].[DestinationCidrBlock||DestinationPrefixListId,GatewayId||NatGatewayId]}'
```

Note `AvailableIpAddressCount` of 251 for each /24 and 4,091 for each /20 (5 reserved).

**Step 4.** Launch a test instance in a private subnet with SSM access (no SSH, no public IP) and verify egress:

```bash
# from an SSM session on the private instance
curl -s https://checkip.amazonaws.com      # prints the NAT gateway's EIP for that AZ
ip route                                   # default via 10.0.x.1 (the VPC router)
dig +short s3.us-east-1.amazonaws.com      # public S3 IPs, but routed through the gateway endpoint
```

Compare the printed IP with `aws ec2 describe-nat-gateways`.

**Step 5.** Use Reachability Analyzer to check that the app SG can reach the DB SG on 5432 but the Internet cannot (Console: VPC > Reachability Analyzer, source = IGW, destination = an ENI in the DB subnet). The analysis should report "not reachable" and name the missing route.

**Step 6.** Destroy everything: `terraform destroy`.

## Key takeaways

- A VPC is regional; subnets are AZ-scoped; a subnet is public only because its route table points to an IGW.
- The IGW performs 1:1 NAT for public IPs; NAT gateways provide outbound-only PAT for private subnets and should be one per AZ.
- NAT gateway data charges are often avoidable: use free S3/DynamoDB gateway endpoints and interface endpoints for other services.
- Peering is non-transitive and needs non-overlapping CIDRs and routes on both sides; Transit Gateway provides hub-and-spoke routing with route-table segmentation.
- PrivateLink exposes a service through an ENI in the consumer VPC and tolerates overlapping CIDRs.
- Flow logs show ACCEPT/REJECT per flow; Reachability Analyzer finds blocking configuration without sending packets.
- Azure and GCP have direct equivalents; GCP's VPC is global and its firewall rules target tags or service accounts.
- Derive subnet plans with `cidrsubnet()` so the whole layout follows from one CIDR.
