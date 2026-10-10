# E8 — Steady-state platform overhead

**Date:** 2026-10-11
**Cluster:** 7 nodes, 36 cores, ~49 GiB RAM

| Node | Role | Hardware | OS | Cores | RAM |
|---|---|---|---|---|---|
| server-1/2/3 | control-plane, etcd | Raspberry Pi 5 (8 GB) | Ubuntu 24.04.5 | 4 each | 7.75 GiB each |
| agent-7 | agent | Raspberry Pi 5 (**4 GB**) | Ubuntu 24.04.5 | 4 | 3.69 GiB |
| agent-10 | agent | Raspberry Pi 5 (8 GB) | Ubuntu 24.04.5 | 4 | 7.75 GiB |
| agent-67 | agent | **Mac mini (Apple Silicon), solar-powered** | Fedora Asahi Remix 44 | 8 | 7.37 GiB |
| mac-mini-agent | agent | Mac mini (Apple Silicon) | Fedora Asahi Remix 44 | 8 | 7.37 GiB |

**Method:** Prometheus instant queries against `monitoring-kube-prometheus-prometheus` (10.43.239.51:9090), issued from server-1
**State during measurement:** idle — all pods Running, `camera-ingest` HPA at its floor of 1 replica, 0% CPU

Answers: *what does the platform cost before any tenant work is done?* On three Pi 5s this is not an academic question — it determines how much of the hardware is actually available to applications.

## What we did, and how

The cluster was left at rest: no load generator running, `camera-ingest` scaled to its HPA minimum, no playbook in flight. Four instant queries were issued directly against Prometheus' HTTP API rather than through Grafana, so the figures are raw series values rather than rendered panels:

```bash
P=10.43.239.51:9090
q(){ curl -s --get "http://$P/api/v1/query" --data-urlencode "query=$1"; }

# per-node CPU, 5-minute average
q '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'

# per-node memory actually committed (excludes reclaimable cache)
q '(1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes)*100'

# per-namespace CPU and working-set memory
q 'sum by(namespace)(rate(container_cpu_usage_seconds_total{container!=""}[5m]))'
q 'sum by(namespace)(container_memory_working_set_bytes{container!=""})'
```

Memory is reported from `MemAvailable`, not `MemFree`, so page cache that the kernel can reclaim is not counted as used. Container memory uses working set rather than RSS, which is the figure the kubelet actually evicts on.

## What we measured

### Per node

| Node | Role | CPU | Memory used | of total |
|---|---|---|---|---|
| server-1 | control-plane, etcd | 5.0% | 1.63 GiB | 21.0% of 7.8 |
| server-2 | control-plane, etcd | 6.3% | 2.19 GiB | 28.2% of 7.8 |
| server-3 | control-plane, etcd | 8.4% | 2.15 GiB | 27.7% of 7.8 |
| agent-7 | agent | 4.5% | 0.52 GiB | 14.0% of 3.7 |
| agent-10 | agent | 0.8% | 0.51 GiB | 6.6% of 7.8 |
| agent-67 (Mac mini, solar) | agent | 0.6% | 1.86 GiB | 25.3% of 7.4 |
| mac-mini-agent | agent | 0.6% | 1.90 GiB | 25.8% of 7.4 |

### Per namespace

| Namespace | CPU (mCPU) | Memory (MiB) | Pods |
|---|---|---|---|
| **monitoring** | **154.7** | **1457.3** | 12 |
| kube-system | 18.4 | 381.4 | 12 |
| argocd | 22.4 | 381.1 | 7 |
| gitea | 2.4 | 143.4 | 1 |
| whoami | 0.8 | 22.4 | 3 |
| console | 0.9 | 13.6 | 1 |
| workload | 1.7 | 3.5 | 1 |
| **Total** | **201.4** | **2402.7** | 37 |

## What this proves

**The platform is cheap in CPU and expensive in memory.** At rest the entire platform draws **201 mCPU — about 0.2 of a single core** spread across 36 ARM cores. CPU is not a constraint at idle, which is consistent with Test 3's finding that CPU sat at 2–8% even while disks saturated.

Memory is the binding resource: **2.4 GiB of working set**, and the control-plane nodes are each committing 1.6–2.2 GiB before any tenant workload arrives.

**The observability stack costs more than everything it observes.** Monitoring alone is **77% of platform CPU and 61% of platform memory**:

| | monitoring | everything else |
|---|---|---|
| CPU | 154.7 mCPU | 46.7 mCPU |
| Memory | 1457.3 MiB | 945.4 MiB |

For scale, Gitea — which holds every repository *and* every container image for the whole cluster, i.e. the actual source of truth — runs in **143 MiB**, about a tenth of the monitoring stack.

This is a real design tension on constrained hardware, and it is worth stating plainly rather than hiding: the cluster spends most of its idle budget watching itself. `kube-prometheus-stack` ships with a retention and scrape configuration tuned for a datacentre, not for three Pis. `prometheus_retention` is already reduced to 7d in `group_vars/all.yml`; scrape interval and the set of enabled exporters were not tuned.

**Headroom is adequate but not generous.** With ~7.8 GiB per Pi 5 and ~2.2 GiB committed on the busiest control-plane node, roughly 5.5 GiB per node remains. The `workload` namespace quota (32 CPU / 8 GiB requests) is far above what the hardware can actually supply, which is intentional — it is a guard against typos, not a capacity limit.

### Incidental finding — Asahi nodes carry ~1.4 GiB of fixed OS overhead

The two agents committing ~1.9 GiB are both Apple Silicon Mac minis; the two committing ~0.5 GiB are both Raspberry Pi 5s. Every one of these four nodes runs **exactly one pod** — a `node-exporter` — so the difference is not workload:

| Node | OS | Kernel | Pods | Memory committed |
|---|---|---|---|---|
| agent-7 | Ubuntu 24.04.5 | `6.8.0-1064-raspi` | 1 | **0.52 GiB** |
| agent-10 | Ubuntu 24.04.5 | `6.8.0-1064-raspi` | 1 | **0.51 GiB** |
| agent-67 (solar) | Fedora Asahi Remix 44 | `7.1.6-400.asahi...+16k` | 1 | **1.86 GiB** |
| mac-mini-agent | Fedora Asahi Remix 44 | `7.0.13-400.asahi...+16k` | 1 | **1.90 GiB** |

**The Apple Silicon nodes cost roughly 1.4 GiB more at idle than the Pis, before running anything.** On a 7.4 GiB machine that is ~19% of RAM consumed by the base system alone.

Two contributing factors, not separated here: Fedora Server ships a heavier default service set than Ubuntu Server, and — visible in the kernel string — **Asahi runs a 16 KiB page size** (`+16k`) against the Pi kernels' 4 KiB. Larger pages inflate every rounded-up allocation, so some of this gap is accounting rather than waste.

This is worth connecting to Test 1, which recorded anomalously low Mac mini write IOPS (319) and listed page size among the candidate explanations. The same 16 KiB page size appears on both sides of that question; a single page-size-aware re-test would speak to both findings at once.

## Caveats

- **Instant values, n=1.** These are a single snapshot at idle, not averages over a representative period. CPU uses a 5-minute rate window; memory is a point reading.
- **"Idle" is not zero.** ArgoCD continues reconciling on its timer and Prometheus continues scraping throughout, so this measures the platform's *resting* cost, which is the useful figure, rather than a true floor.
- Per-namespace totals exclude pod overhead (pause containers, kubelet) and so sum to less than the node-level figures.

## Reproduction

```bash
# from any control-plane node
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
P=$(k3s kubectl get svc -n monitoring monitoring-kube-prometheus-prometheus \
      -o jsonpath='{.spec.clusterIP}'):9090
curl -s --get "http://$P/api/v1/query" \
  --data-urlencode 'query=sum by(namespace)(container_memory_working_set_bytes{container!=""})'
```

Prometheus' ClusterIP is reachable from the node itself via kube-proxy's iptables rules; no port-forward is needed.
