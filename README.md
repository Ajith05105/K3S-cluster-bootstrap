# k3s Cluster Bootstrap Playbook

Automated bare-metal K3s cluster deployment on Raspberry Pi 5. One command takes you from powered-off Pis to a self-healing HA control plane that manages itself after the laptop is unplugged.

## How It Works

```
ansible-playbook playbooks/site.yml
        │
        ├── preflight.yml              → build custom ARM64 images on laptop
        ├── prepare_nodes.yml          → DHCP → discover Pis → static IP + cgroups
        ├── bootstrap_control_plane.yml → install k3s HA cluster (serial)
        └── deploy_platform.yml        → deploy dnsmasq + ansible-runner into cluster
```

After `deploy_platform.yml` completes, the laptop can be unplugged. The cluster runs a **dnsmasq pod** as its own DHCP server and an **ansible-runner pod** that auto-joins new nodes when they boot — no human intervention needed.

## Requirements

**Bootstrap laptop** (Ubuntu 24.04 or macOS):
- Ansible: `pip install ansible`
- Podman: `apt install podman`
- QEMU for ARM64 cross-builds: `apt install qemu-user-static binfmt-support`
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

Edit `all.yml` — key values:

```yaml
ansible_user: pi
ansible_ssh_private_key_file: ~/.ssh/id_ed25519
ansible_runner_private_key: ~/.ssh/ansible_runner_key
ansible_runner_public_key: ~/.ssh/ansible_runner_key.pub

k3s_token: <any-long-random-string>

bootstrap_interface: enxa0cec892dc0f   # your USB ethernet adapter — check with: ip a
gateway_ip: 192.168.1.1  # IP to assign your laptop on that interface
dhcp_range_start: 192.168.1.200
dhcp_range_end: 192.168.1.254
subnet_mask: 255.255.255.0
```

> `all.yml` and `hosts.yml` are gitignored — never committed. They contain secrets and environment-specific config.

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
ansible-playbook playbooks/preflight.yml          # build images only
ansible-playbook playbooks/prepare_nodes.yml      # DHCP + discovery + node prep
ansible-playbook playbooks/bootstrap_control_plane.yml  # k3s install
ansible-playbook playbooks/deploy_platform.yml    # in-cluster services
```

## Self-Healing Agent Join

Once deployed, adding a new node requires zero manual steps:

1. Flash a new Pi — include both SSH keys in `user-data`
2. Set hostname: `server` in name → control plane, anything else → agent
3. Plug into switch and power on

The **lease-watcher** sidecar detects the new DHCP lease. The **ansible-runner** pod SSHes in and joins it to the cluster. Check logs:

```bash
kubectl logs -n kube-system -l app=ansible-runner -f
kubectl exec -n kube-system -l app=dnsmasq -- cat /var/lib/dnsmasq/dnsmasq.leases
```

## Project Structure

```
.
├── playbooks/
│   ├── site.yml                     # entry point
│   ├── preflight.yml                # build custom ARM64 images
│   ├── prepare_nodes.yml            # DHCP, discovery, node prep
│   ├── bootstrap_control_plane.yml  # k3s install
│   └── deploy_platform.yml          # in-cluster dnsmasq + ansible-runner
├── roles/
│   ├── laptop_dhcp/                 # dnsmasq DHCP on bootstrap laptop
│   ├── node_prep/                   # static IP, cgroups, reboot
│   ├── copy_binaries/               # copy k3s binary + images to nodes
│   ├── k3s_server/                  # install k3s control plane
│   ├── platform_configmaps/         # ConfigMaps for dnsmasq config + node registry
│   ├── deploy_dnsmasq/              # deploy in-cluster DHCP pod
│   └── deploy_ansible_runner/       # deploy in-cluster Ansible runner pod
├── custom_images/
│   ├── dnsmasq/                     # in-cluster DHCP server (ARM64)
│   ├── lease-watcher/               # watches leases, triggers agent join
│   └── ansible-runner/              # Ansible + SSH, runs inside cluster
├── manifests/
│   ├── dnsmasq/                     # Deployment + ServiceAccount
│   └── ansible-runner/              # Deployment + ServiceAccount
├── scripts/
│   └── discovery.py                 # reads dnsmasq leases → writes hosts.yml
└── inventories/production/
    ├── hosts.yml                    # auto-generated by discovery — gitignored
    └── group_vars/
        ├── all.yml                  # your config — gitignored
        └── all.yml.example          # start here
```

## Known Limitations

- `dnsmasq-config.yaml.j2` has a hardcoded DHCP range (`192.168.1.101–199`) and interface (`eth0`) — these should become variables in `all.yml`
- `node_prep` hardcodes `eth0` as the Pi ethernet interface name — may differ on some hardware
- `bootstrap_control_plane.yml` downloads the latest k3s with no pinned version — pin `k3s_version` in `all.yml` for reproducible deployments
- Joining servers use server-1's raw IP rather than a VIP — for true HA the join URL should point to a kube-vip virtual IP

## Hardware

Tested on:
- Raspberry Pi 5 (ARM64, 8GB)
- Ubuntu Server 24.04 LTS
- Bootstrap laptop: Ubuntu 24.04 MATE with TP-Link UE300 USB ethernet adapter