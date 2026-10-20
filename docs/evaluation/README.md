# Evaluation

Index for the project's measured results. Two sources:

- **[../cluster-performance-testing.md](../cluster-performance-testing.md)** — Tests 1–9, findings A–B, limits L1–L2
- **This directory** — the E-series, filling gaps the above did not cover

Every result follows the same structure: *what we did and how → what we measured → what this proves.*

## Combined results

| # | Result | Where |
|---|---|---|
| 1 | Storage varies **~90×** between node types on the same StorageClass | [perf](../cluster-performance-testing.md) |
| 2 | etcd WAL fsync **p99 16–32 ms**, mean **2.49 ms** over 7.3M fsyncs | [perf](../cluster-performance-testing.md) |
| 3 | Disk-saturated control-plane node leaves in **~2 min**, self-recovers in **~9 min**, API never degraded | [perf](../cluster-performance-testing.md) |
| 4 | Graceful VIP failover: **0 of 178 probes** failed | [perf](../cluster-performance-testing.md) |
| 5 | Full power loss: recovers **unattended**, via systemd + on-disk etcd | [perf](../cluster-performance-testing.md) |
| 6 | Hard node failure (SIGKILL): **5.77 s** VIP outage | [perf](../cluster-performance-testing.md) |
| 7 | Repeatability n=3: **±1–11%** spread; 90× ratio reproduces | [perf](../cluster-performance-testing.md) |
| 8 | Capacity recovery: backlog cleared **11 s** after a node went Ready — scheduler, **not** provisioning | [perf](../cluster-performance-testing.md) |
| 9 | Node power cut under load: **52 s** outage, of which **47 s (90%) was detection** | [perf](../cluster-performance-testing.md) |
| A | Airgap images are **not uniformly distributed** across nodes | [perf](../cluster-performance-testing.md) |
| B | Running VIP (`.60`) **does not match** declared config (`.50`) | [perf](../cluster-performance-testing.md) |
| **E4** | Resource isolation **enforced**; network and privilege isolation **absent** — tenant pods reach each other and the platform, as root | [E4](E4-tenant-isolation.md) |
| **E5a** | GitOps drift repaired in **1.5 s** first time, degrading exponentially to a **~300 s** ceiling under repetition | [E5a](E5a-gitops-drift-correction.md) |
| **E6** | Gitea repos + DB restore in **~30 s**, `git fsck` clean, DB loads fully — but **application images are in no backup** and the dumps are **world-readable with secrets** | [E6](E6-backup-restore.md) |
| **E7** | Simultaneous unclean reset of **all 3 control-plane nodes**: full service back in **138 s**, unattended, 35/35 pods restored, clocks survived | [E7](E7-full-cluster-power-loss.md) |
| **E8** | Platform costs **201 mCPU / 2.4 GiB** at rest; monitoring alone is **77% of CPU, 61% of memory** | [E8](E8-steady-state-overhead.md) |
| **E8b** | Asahi Mac minis carry **~1.4 GiB more** idle OS overhead than the Pis, running identical pods | [E8](E8-steady-state-overhead.md) |

## Raw data

[`data/`](data/) holds the series behind Tests 8 and 9, pulled from the console's
`GET /api/files` endpoint:

| File | Contents |
|---|---|
| `t1run6-capacity-recovery.csv` | 393 rows, per-second, the capacity-recovery run |
| `t2run2-power-cut.csv` | 383 rows, per-second, the node power-cut run |
| `all-test-marks.csv` | 82 operator marks across all runs — the experimental log |
| `e7-fullcluster-power-loss.csv` | 68 samples, 1 Hz, the control-plane reset and recovery |

Columns: `sent, processed, failed, failed_busy, failed_timeout, latency_mean_ms, latency_p95_ms, desired_pods, ready_pods, pending_pods, ready_nodes, per_node`.

## Still outstanding

Ordered by value, with the two destructive items last.

| Item | Status | Note |
|---|---|---|
| **Agent join time** (lease-watcher → ansible-runner, full cycle) | **done by Ajith** | Pi **169 s** (107.7 s to Ready + 61.6 s to first workload); Mac mini **30 s** (12.5 + 17.5). Mean of 4 runs each, zero manual steps, zero failures. Supersedes the 3–8 min estimate. **Not yet written up here — needs Ajith's raw timings.** |
| **Backup restore** | **done** — [E6](E6-backup-restore.md) | Restore proven in ~30 s. L1 rewritten. Remaining sub-gap: a *running* Gitea serving restored data was not stood up. |
| **E5b — kube-vip drift** | **not done** (deliberately skipped) | Confirmed safe to attempt: `platform-kube-vip` manages the RBAC and cloud-provider but **not** the `kube-vip-ds` DaemonSet, so ArgoCD will not restore it. Save the manifest first — deleting it drops the `.61` service VIP and with it all ingress. |
| **Timed full power loss** | **done** — [E7](E7-full-cluster-power-loss.md) | 138 s to full recovery, no corruption, no intervention. Remaining variant: a cut taken *under load* rather than idle. |
| **Full rebuild from wiped nodes** (RQ1) | **not done** | Destroys etcd and Gitea's PVC. Cannot be run remotely — `site.yml` includes `prepare_nodes.yml`, which needs laptop DHCP on the cluster's own L2 segment. |

## Access

The cluster is reachable through the Tailscale gateway rather than the LAN:

```bash
ssh -i ~/.ssh/id_ed25519 -p 2203 pi@100.82.160.34   # server-1 (2201=server-2, 2202=server-3)
curl -H "Host: console.cluster.local" http://100.82.160.34/api/files
```
