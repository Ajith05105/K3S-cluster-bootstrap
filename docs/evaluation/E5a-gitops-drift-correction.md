# E5a — GitOps drift correction latency

**Date:** 2026-10-11
**Target:** `Deployment/ansible-runner` in `kube-system`, managed by the `platform-ansible-runner` ArgoCD Application
**Method:** delete the Deployment, poll until ArgoCD recreates it and it reports an available replica. Repeated 5×.
**Clock:** all timestamps taken on server-1, the same host issuing the deletes — no cross-machine correlation (see Test 6's skew note).

Answers part of RQ2: *when the live cluster drifts from the declared state, how fast does it converge back?*

## What we did, and how

`platform-ansible-runner` has automated sync with self-heal enabled, which is what makes this measurable:

```
$ kubectl get application platform-ansible-runner -n argocd -o jsonpath='{.spec.syncPolicy}'
{"automated":{"prune":true,"selfHeal":true}}
```

The Deployment was chosen deliberately: it is a genuine platform component (the agent auto-join runner), it is owned end-to-end by ArgoCD, and its loss is non-destructive — while it is gone, no *new* node can self-join, but nothing already running is affected.

Each iteration:

```bash
kubectl wait --for=condition=Available deploy/ansible-runner -n kube-system --timeout=120s
sleep 5                                  # let the app settle to Synced
t0=$(date +%s.%N)
kubectl delete deploy ansible-runner -n kube-system --wait=false
# poll at 1s until the object exists again, then until availableReplicas=1
```

Two instants are recorded per run: **recreated** (ArgoCD has re-applied the manifest) and **available** (the pod is actually serving). The gap between them is pod startup, not GitOps latency.

## What we measured

| Run | Recreated | Available | Δ (pod start) |
|---:|---:|---:|---:|
| 1 | **1.5 s** | 8.7 s | 7.2 s |
| 2 | 11.9 s | 21.1 s | 9.2 s |
| 3 | 39.2 s | 40.5 s | 1.3 s |
| 4 | 155.6 s | 156.9 s | 1.3 s |
| 5 | **293.6 s** | 293.8 s | 0.2 s |

Final state after the sequence: `platform-ansible-runner   Synced   Healthy`, Deployment `1/1`. The cluster self-corrected every time without intervention.

## What this proves

**Drift is corrected automatically — but the latency is not constant, and it degrades by nearly 200× under repetition.**

The first deletion was repaired in **1.5 seconds**, with the pod serving **8.7 seconds** after it was destroyed. That is the figure most people would quote, and on its own it supports a strong self-healing claim.

It is also the best case, and only the best case. Each subsequent deletion took roughly **three times longer** to notice than the one before:

```
1.5s  →  11.9s  →  39.2s  →  155.6s  →  293.6s
      ×7.9     ×3.3      ×4.0       ×1.9
```

That is an exponential backoff with a growth factor of roughly 3, converging on a ceiling just under **300 s**. The cluster runs stock settings — `argocd-cmd-params-cm` sets no backoff keys, the application controller carries no backoff flags, and the vendored chart's `values.yaml` contains none — so this is ArgoCD's default self-heal backoff behaviour.

**On the base duration:** run 1's 1.5 s should not be read as the configured base. ArgoCD reacts to the first drift through its resource watch before any backoff has accumulated, so run 1 measures watch latency, not the backoff floor. The growth factor (~3) and the cap (~300 s) are the two parameters this data actually constrains; the configured base is not recoverable from it. Cite the measured curve, not an assumed constant.

**Why this matters for the self-healing claim.** The honest statement is not "ArgoCD repairs drift in under 2 seconds." It is:

> Isolated drift is repaired in seconds. *Repeated* drift against the same resource is repaired progressively more slowly, up to a ceiling of about 5 minutes.

Three practical consequences:

1. **An operator fighting the system loses slowly.** Someone hand-editing a GitOps-managed resource repeatedly — exactly what a person debugging under pressure does — pushes ArgoCD into maximum backoff and then sees their changes persist for minutes. That looks like self-healing being broken, when it is working as designed.
2. **It is a real, if modest, security property.** An attacker who can delete a resource can keep it deleted for up to ~5 minutes per cycle by re-deleting it, and the window *widens* with each attempt rather than narrowing.
3. **Pod startup stops mattering.** In runs 3–5 the Deployment went from recreated to available in ~1 s, because the image was already warm on the node. Early runs paid 7–9 s. So after the first repair, essentially all the latency is ArgoCD's backoff, not Kubernetes.

**Also worth noting:** the *detection* side is not the bottleneck in run 1. ArgoCD noticed and re-applied in 1.5 s, which means it is watching rather than waiting for its 3-minute refresh. The backoff is a deliberate throttle on repair, not slow observation.

## Caveats

- **n=1 per backoff step.** The sequence was run once; the five values are five different backoff states, not five samples of the same state. Re-running would confirm the curve but costs another ~8 minutes of runner downtime.
- The backoff state is per-application and decays once the resource stays healthy, so these figures describe drift *in quick succession*. Drift spaced hours apart would each see the run-1 latency.
- Only one resource kind was tested. A Deployment is cheap to re-apply; a CRD or a resource with a slow admission path could differ.
- Measures ArgoCD's reaction, not Git propagation. Drift here was made directly against the API server; drift introduced by a Git commit additionally waits on repo polling.

## Reproduction

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
for i in 1 2 3 4 5; do
  kubectl wait --for=condition=Available deploy/ansible-runner -n kube-system --timeout=120s
  sleep 5
  t0=$(date +%s.%N)
  kubectl delete deploy ansible-runner -n kube-system --wait=false
  until [ "$(kubectl get deploy ansible-runner -n kube-system \
             -o jsonpath='{.status.availableReplicas}' 2>/dev/null)" = "1" ]; do sleep 1; done
  echo "run $i: $(echo "$(date +%s.%N) - $t0" | bc)s"
done
```

Allow ~10 minutes: the later iterations are dominated by the backoff itself.
