# k3s Cluster Bootstrap Playbook

Automated bare-metal K3s cluster deployment on Raspberry Pi 5, with a full
GitOps platform (Gitea + ArgoCD) and a multi-tenant security model on top.
One command takes you from powered-off Pis to a self-healing, HA control
plane; a second gets Gitea, ArgoCD, and tenant isolation running, all
version-controlled and self-syncing after that.

## How It Works

```
ansible-playbook playbooks/site.yml
        │
        ├── preflight.yml               → build custom ARM64 images + vendor Helm charts on laptop
        ├── prepare_nodes.yml           → DHCP → discover Pis → static IP + cgroups
        ├── bootstrap_control_plane.yml → install k3s HA cluster + kube-vip (serial)
        └── deploy_platform.yml         → Gitea, ArgoCD, registry mirror, tenants, GitOps bootstrap
```

After `deploy_platform.yml` completes, the laptop can be unplugged. The cluster
runs its own DHCP server, auto-joins new nodes when they boot, and syncs
itself against a Git repo — no human intervention needed for day-to-day
operation.

For tenant-only changes (adding/editing a tenant, without touching the rest
of the platform), there's a fast path that skips the full redeploy:

```bash
ansible-playbook playbooks/sync_tenants.yml
```

## Requirements

**Bootstrap laptop** (Ubuntu 24.04 or macOS):
- Ansible: `pip install ansible`
- Podman: `apt install podman`
- QEMU for ARM64 cross-builds: `apt install qemu-user-static binfmt-support`
- Helm: `curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash`
- USB ethernet adapter connected to the Pi switch

**Raspberry Pi 5 nodes**:
- Ubuntu Server 24.04 LTS (64-bit), flashed via Raspberry Pi Imager
- SSH public key injected at flash time
- Passwordless sudo configured in `user-data` (see below)
- Ethernet connected to switch — no WiFi

**Two SSH key pairs**:
- `~/.ssh/id_ed25519` — your personal key, used by Ansible during bootstrap
- `~/.ssh/ansible_runner_key` — a separate key stored as a Kubernetes Secret, used by the in-cluster runner to auto-join new agents

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ansible_runner_key -N ""
```

## Flashing the Pis

In Raspberry Pi Imager, select:
- Device: Raspberry Pi 5
- OS: Ubuntu Server 24.04 LTS (64-bit)

Under **Customisation**:
- Hostname: include `server` in the name for control plane nodes (e.g. `server-1`, `server-2`). Anything else becomes a worker.
- Username: `pi` — must be `pi`, this is expected by the playbook
- Skip WiFi

Under **Remote Access**:
- Enable SSH → public-key only
- Paste `~/.ssh/id_ed25519.pub`

Before ejecting, open `user-data` on the `system-boot` partition and add passwordless sudo and both public keys:

```yaml
users:
  - name: pi
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - ssh-ed25519 AAAA...   # id_ed25519.pub — your personal key
      - ssh-ed25519 BBBB...   # ansible_runner_key.pub — for in-cluster runner
```

## Configuration

```bash
cp inventories/production/group_vars/all.yml.example inventories/production/group_vars/all.yml
```

`all.yml` has grown a lot since this project started — every pinned version,
credential, and network setting the roles need lives there now. Key groups:

```yaml
# Network — laptop interface connected to the Pi switch
bootstrap_interface: enxa0cec892dc0f   # check with: ip a
gateway_ip: 192.168.1.1                # your LAN's actual default route
dhcp_range_start: 192.168.1.200
dhcp_range_end: 192.168.1.254

# kube-vip — HA control-plane VIP + LoadBalancer IP range
kube_vip_interface: eth0
kube_vip_vip: 192.168.1.50

# Everything pulled/vendored during preflight is version-pinned here —
# helm_version, gitea_version, gitea_chart_version, argocd_version,
# argocd_chart_version, dex_version, redis_version,
# kube_vip_cloud_provider_version — see all.yml.example for the full list
```

> `all.yml` and `hosts.yml` are gitignored — never committed. They contain
> secrets and environment-specific config. So are the vendored chart
> directories under `manifests/*/` — those are fetched fresh by
> `preflight.yml` (idempotent, skips if already present), not hand-edited or
> checked in, the same way you wouldn't commit `node_modules/`.

## Usage

1. Plug your USB ethernet adapter into the switch. Do **not** power on the Pis yet.

2. Assign your laptop a static IP on the ethernet interface:
   ```bash
   sudo ip addr add 192.168.1.100/24 dev enxa0cec892dc0f
   ```

3. Run:
   ```bash
   ansible-playbook playbooks/site.yml
   ```

4. When prompted, power on all Pis then press Enter. The rest is automatic.

To run individual stages:

```bash
ansible-playbook playbooks/preflight.yml               # build images + vendor charts only
ansible-playbook playbooks/prepare_nodes.yml           # DHCP + discovery + node prep
ansible-playbook playbooks/bootstrap_control_plane.yml # k3s + kube-vip install
ansible-playbook playbooks/deploy_platform.yml         # Gitea, ArgoCD, tenants, GitOps
ansible-playbook playbooks/sync_tenants.yml            # tenants only, fast path
```

## Self-Healing Agent Join

Once deployed, adding a new node requires zero manual steps — confirmed
working in practice, not just in theory:

1. Flash a new Pi — include both SSH keys in `user-data`
2. Set hostname: anything without `server` in it → joins as an agent
3. Plug into switch and power on

The **lease-watcher** sidecar detects the new DHCP lease. The **ansible-runner**
pod — a plain Python/paramiko script, despite the name, not literally
Ansible — SSHes in and joins it to the cluster. Check logs:

```bash
kubectl logs -n kube-system -l app=ansible-runner -f
kubectl exec -n kube-system -l app=dnsmasq -- cat /var/lib/dnsmasq/dnsmasq.leases
```

Currently handles new **agents** only — adding another control-plane node
still goes through the manual `bootstrap_control_plane.yml` path.

## GitOps & Platform Services

Everything from here down runs *inside* the cluster and manages itself via
Git, not via `kubectl` or Ansible re-runs.

**Gitea** (`manifests/gitea/`) is both the git server and the container
registry for the whole cluster. It's deployed via its official Helm chart,
vendored offline during `preflight.yml`. Its own image gets imported
directly from a tarball, since — chicken-and-egg — there's no registry to
pull it from until it's already running.

**Registry mirroring**: every node trusts `gitea.cluster.local:3000` as a
registry mirror (`configure_registry`). ArgoCD, Gitea's own images, and
dex/redis are all crane-pushed into it during deploy, so the cluster never
depends on live internet access to pull its own platform images. Anything a
*tenant* pulls (their own app images) works the same way, through their own
Gitea registry namespace.

**ArgoCD** (`manifests/argocd/`) is deployed via Helm, images pointed at the
Gitea mirror, pinned to control-plane nodes. Credentials for pulling from
Gitea are handled via a Kubernetes `imagePullSecret` rather than containerd's
own registry-auth mechanism — that mechanism turned out to have a real,
currently-unresolved bug on this k3s version (confirmed against multiple
open `k3s-io/k3s` issues), so this sidesteps it rather than fighting it.

**`cluster-config`**, a private repo Gitea hosts for itself, is what ArgoCD
actually watches. `bootstrap_gitops` force-pushes this repo's own
`manifests/` directory into it on every run — so the ansible repo is always
the real source of truth; Gitea is a mirror of it, never edited directly.

ArgoCD self-adopts `dnsmasq`, `kube-vip`, and `ansible-runner` as genuine,
self-healing Applications watching their own paths in `cluster-config` — the
same pattern proven with tenants. Gitea and ArgoCD's own installation are the
two remaining components managed imperatively by Ansible, and that's a
permanent floor, not a gap: neither can GitOps-manage its own first
deployment before it exists to do the managing.

**Pushing images to Gitea's registry** is always a command, for any
registry anywhere — no registry, Gitea included, offers a browser upload for
image layers, since the push protocol negotiates layers individually rather
than sending one file. From any machine with `gitea.cluster.local` in its
hosts file (the bootstrap laptop gets this automatically; a tenant's own
machine needs the same one-time entry), pushing is the same standard
workflow as any other registry:

````bash
podman login gitea.cluster.local -u <tenant> -p <password>
podman push <image> gitea.cluster.local/<tenant>/app:<tag>
````

Since Gitea serves plain HTTP (see Known Limitations), any pushing machine
needs to explicitly trust it as an insecure registry first:

````bash
# podman: /etc/containers/registries.conf
[[registry]]
location = "gitea.cluster.local"
insecure = true

# docker: /etc/docker/daemon.json
{ "insecure-registries": ["gitea.cluster.local"] }
```

## Multi-Tenant Security Model

Tenants interact with the cluster **only** through Git — no `kubectl`, no
SSH, no direct cluster access at all. Their blast radius is exactly their
own namespace, enforced by four independent controls, each individually
tested against a real attack, not just declared:

- **PodSecurity** (`restricted` profile) — no root, no privilege escalation, capabilities dropped. Rejects a non-compliant pod outright, before it's even created.
- **ResourceQuota + LimitRange** — a hard per-namespace ceiling, plus sane per-container defaults so tenants don't need to know Kubernetes resource syntax. Both are configurable per tenant (see below).
- **NetworkPolicy** — default-deny, with narrow allows for DNS, inbound via Traefik, and outbound to the Gitea registry. Verified empirically: a tenant pod is refused reaching another namespace; the same command from a platform pod succeeds.
- **AppProject** — the GitOps-specific boundary. Locks a tenant's `Application` to their own repo and namespace, and blocks them from creating cluster-scoped resources *or* tampering with their own quota/NetworkPolicy from inside their own repo. Tested directly: a real `git push` containing a cross-namespace Deployment and a cluster-admin `ClusterRole` was rejected by ArgoCD by name, with the specific rule that caught each one.

**Onboarding a tenant** is a one-line file, not a folder of duplicated YAML:

```bash
cat > manifests/tenants/declarations/bob.yaml << EOF
tenantName: bob
memRequest: "128Mi"
memLimit: "256Mi"
cpuRequest: "100m"
cpuLimit: "200m"
EOF
git add manifests/tenants/declarations/bob.yaml
git commit -m "feat: onboard bob"
ansible-playbook playbooks/sync_tenants.yml
```

That's rendered through one shared chart (`manifests/tenants/_chart/`) by an
ArgoCD `ApplicationSet`, which also triggers `bootstrap_gitops` to create
bob's Gitea account and personal `app` repo automatically, with a randomly
generated password stored as a Kubernetes Secret — never hardcoded, never
committed, same pattern ArgoCD itself uses for its own admin password.

## Project Structure

```
.
├── playbooks/
│   ├── site.yml                     # full bootstrap entry point
│   ├── preflight.yml                # build images, vendor Helm charts
│   ├── prepare_nodes.yml            # DHCP, discovery, node prep
│   ├── bootstrap_control_plane.yml  # k3s + kube-vip install
│   ├── deploy_platform.yml          # dnsmasq, ansible-runner, Gitea, ArgoCD, tenants
│   └── sync_tenants.yml             # tenants-only fast path
├── roles/
│   ├── laptop_dhcp/, node_prep/, copy_binaries/, k3s_server/
│   ├── deploy_kube_vip/             # HA VIP + LoadBalancer support
│   ├── platform_configmaps/, deploy_dnsmasq/, deploy_ansible_runner/
│   ├── deploy_gitea/, configure_registry/
│   ├── push_argocd_images_to_gitea/, deploy_argocd/
│   └── bootstrap_gitops/            # cluster-config repo, tenant onboarding
├── custom_images/
│   ├── dnsmasq/, lease-watcher/, ansible-runner/
├── manifests/
│   ├── dnsmasq/, ansible-runner/
│   ├── gitea/                       # values.yaml (chart vendored, gitignored)
│   ├── argocd/                      # values.yaml (chart vendored, gitignored)
│   └── tenants/
│       ├── _chart/                  # shared Helm chart for every tenant
│       └── declarations/            # one file per tenant
├── scripts/discovery.py
└── inventories/production/
    ├── hosts.yml                    # auto-generated — gitignored
    └── group_vars/
        ├── all.yml                  # your config — gitignored
        └── all.yml.example          # start here
```

## Known Limitations

- Joining additional control-plane nodes still uses server-1's raw IP, not kube-vip's VIP — true HA join isn't wired up yet, even though kube-vip itself is running and serving LoadBalancer IPs correctly.
- ArgoCD and Gitea are both served over plain HTTP, no TLS. Deliberate for now, not an oversight — this is a physically access-controlled LAN, not a publicly exposed service, and self-signed certs would add complexity without a real threat they'd defend against here. Worth revisiting with cert-manager + an internal CA if that changes.
- Kubernetes Secrets in etcd are base64-encoded, not encrypted at rest (k3s's default). Every credential this project generates — tenant passwords included — lives there. Enabling `--secrets-encryption` needs reinstalling k3s server-side, so it's a real, separate task, not a quick patch.
- No persistent NTP. Node clocks drift on every reboot (no RTC on these Pis) and nothing auto-corrects it — this has caused real, repeated failures during development. An in-cluster NTP server is planned; running it on the NAT-router Pi instead of inside Kubernetes would be more robust, since it sidesteps the cluster needing correct time to bootstrap the very thing that would give it correct time.
- A tenant's Gitea login password and the credential ArgoCD uses to keep syncing their repo are currently the same value. Decoupling these (a separate access token for ArgoCD) would let a tenant change their own password without silently breaking their sync — not done yet.
- The `ApplicationSet` that generates tenants polls Git on its own ~3 minute timer, separate from a normal Application's refresh. A new or edited tenant can take a few minutes to actually take effect. A Gitea webhook would close this gap if onboarding speed ever matters.
- No backup/snapshot mechanism yet for Gitea's PVC (repos + registry data) — local-path storage, single node, no redundancy. Real HA is likely not worth the complexity here (Gitea's SQLite backend is single-writer regardless); a periodic backup is the right-sized fix and is planned.

## Hardware

Tested on:
- Raspberry Pi 5 (ARM64, 8GB) × 3 control-plane nodes, plus a 4th Pi acting as NAT router for the LAN (not part of the k3s cluster itself)
- Ubuntu Server 24.04 LTS
- Bootstrap laptop: Ubuntu 24.04 with a USB ethernet adapter
