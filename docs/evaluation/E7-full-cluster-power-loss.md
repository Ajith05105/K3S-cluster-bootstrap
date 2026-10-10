# E7 — Simultaneous hard reset of the entire control plane

**Date:** 2026-10-11
**Trigger:** `echo b > /proc/sysrq-trigger` on server-1, server-2 and server-3 at the same instant
**Measurement:** polled once per second from the laptop, over the Tailscale gateway (which is not a cluster node and so survives)
**Outcome:** full unattended recovery in **138 seconds**. No data loss, no manual intervention, no corruption.

Supersedes Test 5, which observed a real power loss but was not instrumented. This is the same failure, timed.

## What we did, and how

### Why sysrq rather than `reboot`

`systemctl reboot` sends SIGTERM, lets etcd flush its WAL and close cleanly, and unmounts filesystems — skipping exactly the failure modes a power cut causes. `echo b > /proc/sysrq-trigger` resets the kernel immediately: no SIGTERM, no unmount, no systemd clock persist. The only thing it does not reproduce is loss of the power rail itself.

That the shutdown really was unclean is confirmed in each node's boot log:

```
EXT4-fs (mmcblk0p2): INFO: recovery required on readonly filesystem
EXT4-fs (mmcblk0p2): write access will be enabled during recovery
EXT4-fs (mmcblk0p2): recovery complete
```

A graceful reboot never produces those lines.

### Pre-flight

Checked before firing, because any of these failing turns the test into an outage with no automatic recovery:

| Check | All three nodes |
|---|---|
| `/proc/sys/kernel/sysrq` | **176** — bit 128 set, so `b` is permitted |
| `systemctl is-enabled k3s` | **enabled** — k3s restarts on boot |
| etcd snapshots on disk | **5 per node** |
| Gitea dumps available | 3 on server-1, 3 on server-2 |
| NTP synchronised | yes, all three |

### Firing them together

Each node was armed with a spin-wait on an **absolute epoch taken from the nodes' own clocks** (which are mutually NTP-synced), so all three fired at the same instant rather than in SSH sequence:

```bash
sudo nohup setsid sh -c 'while [ $(date +%s) -lt $TARGET ]; do sleep 0.2; done; echo b > /proc/sysrq-trigger' &
```

It worked: all three report an identical boot time of **01:58:20**, to the second.

### Measuring without the clock trap

The laptop was **57 seconds ahead of the nodes** at test time — the same skew that produced a false "zero downtime" result in Test 6. Every elapsed figure below therefore comes from the laptop's clock alone (TCP reachability and HTTP status through the gateway). Node-side timestamps are used only for facts that are not durations, such as boot time and log contents.

## What we measured

| Milestone | Elapsed | Detail |
|---|---|---|
| Reset fired | **t+0** | all three simultaneously |
| Kernels booted | **t+12 s** | identical boot timestamp on all three |
| SSH reachable on all three | **t+29 s** | userspace up |
| All 7 nodes `Ready` | < t+111 s | etcd quorum reformed, agents rejoined |
| **Ingress serving HTTP 200** | **t+138 s** | full service restored |

Post-recovery state, against the pre-test baseline:

| | Before | After |
|---|---|---|
| Nodes Ready | 7 | **7** |
| Pods Running | 35 | **35** |
| ArgoCD apps Synced/Healthy | 5 | **5** |
| Gitea | 1/1 Running | **1/1 Running** |

Raw series: [`data/e7-fullcluster-power-loss.csv`](data/e7-fullcluster-power-loss.csv).

## What this proves

**The cluster survives simultaneous, unclean loss of its entire control plane and returns to full service in about two and a half minutes, with no human involvement and nothing lost.**

This is the strongest availability result in this evaluation, and it is worth being precise about *why* it works, because the mechanism is unglamorous: **systemd plus on-disk etcd.** Each node's k3s unit is `enabled`, so it starts at boot; each node's etcd member replays its WAL from local disk; once two of the three members are up, quorum returns and the API server serves again. None of the provisioning pipeline runs. The lease-watcher never fires, ansible-runner is never consulted, and no playbook executes. Test 5 inferred this; this test confirms it under controlled conditions.

**The 138 seconds decomposes into three unequal parts.** Boot is 12 s. Userspace is another 17 s. Everything after that — 109 of the 138 seconds, nearly 80% — is Kubernetes reassembling itself: etcd quorum, the API server, kube-vip electing a VIP holder, and finally traefik becoming ready. The hardware is not the bottleneck; the control plane's own startup sequence is.

**No SD card was corrupted.** Three simultaneous unclean shutdowns, three clean journal recoveries. That is one trial, not a reliability claim — but it is evidence that the failure mode most feared on Pi clusters did not materialise here.

### The clock survived, and that is not luck

This is the result that matters most for the time-drift story. Pi 5s have no battery-backed RTC, and an unclean shutdown denies systemd the chance to persist the current time. Historically this cluster suffered exactly that failure: two control-plane nodes came back 23 days behind, and their API servers rejected ServiceAccount tokens as not-yet-valid.

After this reset, **all three nodes came back with correct, NTP-synchronised time.** No drift, no token rejections, no manual `timedatectl`.

It held because of two deliberate changes in `node_prep` — `systemd-timesyncd` enabled persistently and pointed at `cluster_ntp_server` — combined with that NTP server being reachable at boot. The hardening works.

**But it is conditional, and this session demonstrated the condition.** Between 7 and 9 October the gateway Pi holding `192.168.1.1` left the LAN, and the nodes free-ran: measured **55 seconds** of drift with no way to correct it. Had this test been run then, all three nodes would have come back with stale clocks and nothing to fix them. The correct claim is therefore:

> The cluster survives unclean power loss **provided its NTP source is reachable at boot.** Without it, RTC-less nodes resume at a stale timestamp and the historical token-rejection failure returns.

That single external dependency — one address, on hardware the project does not own — is the quiet precondition behind this otherwise clean result.

## Caveats

- **n=1.** Repeating costs another full outage, and each unclean shutdown carries genuine SD-card risk.
- `sysrq-b` resets the kernel but does not cut the power rail, so it cannot reproduce mid-write tearing on the SD card or brown-out behaviour. It is the closest software equivalent, not an equivalent.
- The cluster was idle. A power cut mid-write, under load, with Gitea being pushed to, could behave differently — in particular for Gitea's SQLite database, which was not being written at the time.
- Recovery was measured to *ingress serving 200*, which is the user-visible definition. Individual workloads may have taken longer to become fully useful.
- Agents were not reset; they stayed up throughout and rejoined the control plane on their own.

## Reproduction

```bash
# verify FIRST - any of these failing means no automatic recovery
cat /proc/sys/kernel/sysrq        # needs bit 128
systemctl is-enabled k3s          # needs 'enabled'

# arm all nodes against an absolute epoch from THEIR clocks, then poll from elsewhere
TARGET=$(( $(ssh node1 date +%s) + 75 ))
for n in node1 node2 node3; do
  ssh $n "sudo nohup setsid sh -c 'while [ \$(date +%s) -lt $TARGET ]; do sleep 0.2; done; echo b > /proc/sysrq-trigger' &"
done
```

Poll from a machine that is **not** part of the cluster, and convert the fire time into that machine's clock before computing any duration.
