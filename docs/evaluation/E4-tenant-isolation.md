# E4 — Tenant isolation: what the current model actually enforces

**Date:** 2026-10-11
**Method:** a scratch namespace primed with the same `LimitRange` and `ResourceQuota` the `manifests/app` chart generates for every app, then probed from inside a pod running in it.
**Cleanup:** namespace deleted; cluster verified 7/7 Ready, all ArgoCD apps Synced/Healthy afterwards.

Answers RQ3 directly: *what isolation does admin-mediated tenancy actually provide, and what did the September simplification cost?*

## What we did, and how

The namespace was created with exactly the two objects the app chart emits ([manifests/app/templates/](../../manifests/app/templates/)) — `LimitRange` (`max: cpu 1 / memory 1Gi`) and `ResourceQuota` (`cpu 2 / memory 2Gi`) — so the results describe the real tenant model, not a contrived one.

Five probes, each chosen to test one boundary a tenant might cross:

| | Probe | Boundary tested |
|---|---|---|
| a | Pod requesting 2 CPU against a 1-CPU `max` | Per-container ceiling |
| b | Deployment of 6 × 500m against a 2-CPU quota | Namespace-wide ceiling |
| c | HTTP to a Service in another tenant's namespace | Lateral network movement |
| d | `id` inside the container | Privilege / Pod Security |
| e | HTTP and DNS to the public internet | Data egress (the sovereignty claim) |

All test pods were pinned to server-1, because images are not uniformly distributed across nodes (see Incidental Finding A).

## What we measured

### (a) Per-container ceiling — **enforced**

```
Error from server (Forbidden): error when creating "STDIN":
pods "too-big" is forbidden: maximum cpu usage per Container is 1, but limit is 2
```

Rejected at admission. Never scheduled, never ran.

### (b) Namespace quota — **enforced, and degrades gracefully**

Six replicas requested; **exactly four were created** (4 × 500m = the 2-CPU quota), and the Deployment settled at `4/6` with `ReplicaFailure`:

```
Error creating: pods "greedy-b5cd68f5f-nc747" is forbidden: exceeded quota: isolation-test,
requested: limits.cpu=500m,requests.cpu=500m, used: limits.cpu=2,requests.cpu=2,
limited: limits.cpu=2,requests.cpu=2
```

The over-ask was refused; the tenant's already-running pods were untouched and no other namespace was affected.

### (c) Lateral network movement — **NOT prevented**

From a pod in the scratch namespace, to a Service in the `whoami` namespace:

```
$ wget -q -O- http://10.43.75.194/
Hostname: whoami-894d6f8b6-9gzgj
IP: 10.42.8.4
```

Full reachability, no policy in the way.

Reaching Gitea in the platform namespace is more interesting:

```
$ wget -q -O- http://10.43.158.90:3000/api/v1/version
wget: server returned error: HTTP/1.1 403 Forbidden
```

**That 403 is an application-layer refusal, not a network one.** The tenant pod reached Gitea's API and got an HTTP response back; it was Gitea's own authentication that said no. The network path to the platform's source-of-truth is wide open.

### (d) Privilege — **no Pod Security enforcement**

```
$ id
uid=0(root) gid=0(root) groups=0(root),10(wheel)
```

Root, in the `wheel` group, in a namespace with no `pod-security.kubernetes.io/enforce` label and a chart that sets no `securityContext`.

### (e) Internet egress — **blocked, but not by policy**

```
$ wget -T 10 -O- http://1.1.1.1/
wget: download timed out

$ nslookup github.com
Address: 10.43.0.10:53
;; connection timed out; no servers could be reached
```

No egress, and CoreDNS cannot resolve external names either.

## What this proves

**Resource isolation is real and works. Security isolation does not exist.**

| Boundary | Enforced? | By what |
|---|---|---|
| Per-container CPU/memory ceiling | **Yes** | `LimitRange`, at admission |
| Namespace-wide resource ceiling | **Yes** | `ResourceQuota`, at admission |
| Tenant → tenant network | **No** | nothing |
| Tenant → platform network | **No** | only Gitea's own app auth |
| Container privilege | **No** | nothing — pods run as root |
| Data egress to the internet | Effectively | **network topology, not policy** |

The two resource controls do exactly what they claim, and (b) is the more valuable of the pair: a runaway tenant is capped without harming its neighbours or the platform. On three Pi 5s shared between several apps, that is the control that keeps the cluster usable.

Everything else is open. **Any tenant pod can reach any other tenant's pods and the platform's own services, as root.** Compromise one tenant workload and you have the network position and the in-container privilege to attack everything else; only Gitea's application-layer auth stands in the way, and it is the only thing that does.

This is the measured cost of the September simplification. The deleted chart (`7721623`) enforced all four boundaries — `pod-security.kubernetes.io/enforce: restricted` on the namespace, a default-deny `NetworkPolicy` with three scoped allows, plus an ArgoCD `AppProject` that blacklisted tenants from editing their own quota, limits or policies. Two of those four survive today.

### The egress result deserves care

"Nothing can leave the cluster" is **currently true** — and it is load-bearing for the sovereignty argument. But it holds because no route to the internet exists from the pod network, not because anything forbids it. There is no egress `NetworkPolicy`; the cluster is sovereign here by topology.

That distinction matters in two directions. It is honest evidence that data does not leave. It is also **silently reversible**: the day someone enables NAT on the gateway Pi to make a playbook run easier, every tenant pod gains unrestricted outbound access and nothing in the cluster will object or log it. A sovereignty property that depends on an absent route rather than an explicit deny is a property one `iptables` line away from being gone.

### Incidental corroboration — quota blocks replacements during churn

The first attempt at probes (c)–(e) failed:

```
pods "prober" is forbidden: exceeded quota: isolation-test,
used: limits.cpu=2,requests.cpu=2, limited: limits.cpu=2
```

The previous Deployment had been deleted, but its **terminating** pods still counted against the quota, so the new pod was refused until they fully exited.

This independently reproduces the incident recorded in the console's own `deploy/manifests/workload-quota.yaml`, dated 2026-10-09: *"12 pods on a dead node plus 12 others reached exactly 12 CPUs at 500m each, 7 replacements were refused, and Kubernetes did not try again for minutes."*

So this is not a curiosity — **a tightly-sized `ResourceQuota` actively impedes failover**, because the dead node's pods keep their quota reservation through the termination grace period while their replacements are trying to be created. Quota must be sized to hold two full generations of pods, or self-healing stalls exactly when it is needed. That is a direct and non-obvious interaction between RQ2 and RQ3.

## Caveats

- Probes are n=1 each; all are deterministic admission or connectivity outcomes rather than timings, so repetition adds little.
- (e) shows egress blocked from the pod network on this cluster's current topology. It was not tested from a host network pod, which may differ.
- Only `busybox` was used. A deliberately hostile image (hostPath mounts, `hostNetwork`, privileged) would likely also succeed given no PSA, but that was not tested.

## Reproduction

```bash
kubectl create namespace isolation-test
# apply the LimitRange + ResourceQuota from manifests/app/templates/
kubectl run prober -n isolation-test --image=docker.io/rancher/mirrored-library-busybox:1.37.0 \
  --overrides='{"spec":{"nodeName":"server-1"}}' -- sleep 900
kubectl exec -n isolation-test prober -- id
kubectl exec -n isolation-test prober -- wget -q -O- http://<other-ns-svc-ip>/
kubectl exec -n isolation-test prober -- wget -T 10 -O- http://1.1.1.1/
kubectl delete namespace isolation-test
```

Pin to a node that already holds the busybox image; it is not present on every node.
