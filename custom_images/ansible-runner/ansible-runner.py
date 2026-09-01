#!/usr/bin/env python3

import base64
import datetime
import os
import time

import paramiko
from kubernetes import client, config, watch

WORKER_CONFIGMAP = "worker-registry"
NAMESPACE = "kube-system"
SSH_USER = "pi"
SSH_KEY_PATH = "/root/.ssh/ansible_runner_key"

# kube-vip's control-plane VIP, injected by roles/deploy_ansible_runner.
# A self-joined server needs this in its serving cert SANs (same as
# roles/k3s_server does), otherwise kube-vip failing the VIP over to that
# node breaks HTTPS to the API.
KUBE_VIP_VIP = os.environ.get("KUBE_VIP_VIP", "").strip()

# LAN NTP server handed to self-joined agents. They never run node_prep, so
# this is the only place they get a persistent time source.
NTP_SERVER = os.environ.get("NTP_SERVER", "").strip()

# How often to re-check every registry entry against reality. Lower means
# drift (re-flashed board, dead node, failed provision) is noticed sooner, at
# the cost of more SSH probes per node per hour.
RESYNC_INTERVAL = int(os.environ.get("RESYNC_INTERVAL", "300"))

# Paths on the control plane node (mounted into container via hostPath)
K3S_BINARY_PATH = "/usr/local/bin/k3s"
K3S_INSTALL_SCRIPT_PATH = "/home/pi/k3s-install.sh"
K3S_AIRGAP_IMAGES_PATH = "/var/lib/rancher/k3s/agent/images/k3s-airgap-images-arm64.tar.zst"

# Temporary drop locations on worker (pi user can write here)
K3S_BINARY_TMP = "/tmp/k3s"
K3S_INSTALL_SCRIPT_DEST = "/tmp/k3s-install.sh"
K3S_AIRGAP_IMAGES_DEST = "/tmp/k3s-airgap-images-arm64.tar.zst"

# Final system paths on worker
K3S_BINARY_FINAL = "/usr/local/bin/k3s"
K3S_IMAGES_DIR = "/var/lib/rancher/k3s/agent/images"


def load_k8s_client():
    config.load_incluster_config()
    return client.CoreV1Api()


def get_role(hostname):
    """Decide the k3s role from the hostname, failing closed.

    Accepts either ordering so the existing fleet naming (server-1, agent-1)
    and a suffix style (pi5-server, pi4-agent) both work:
        server-* / *-server -> server
        agent-*  / *-agent  -> agent

    Anything else raises. We never fall back to "agent", because guessing a
    role for an unrecognised host is exactly how a control-plane board ends
    up being rebuilt as a worker.
    """
    name = hostname.strip().lower()
    if name.startswith("server-") or name.endswith("-server"):
        return "server"
    if name.startswith("agent-") or name.endswith("-agent"):
        return "agent"
    raise ValueError(
        f"hostname {hostname!r} declares no role "
        "(expected server-*/*-server or agent-*/*-agent)"
    )


def get_join_token(api):
    secret = api.read_namespaced_secret(
        name="k3s-join-token",
        namespace=NAMESPACE
    )
    return base64.b64decode(secret.data["token"]).decode().strip()


def get_control_plane_url(api):
    nodes = api.list_node()
    for node in nodes.items:
        labels = node.metadata.labels or {}
        if "node-role.kubernetes.io/control-plane" in labels:
            for address in node.status.addresses:
                if address.type == "InternalIP":
                    return f"https://{address.address}:6443"
    raise Exception("No control plane node found")


def control_plane_is_healthy(api):
    """Pre-flight check before adding a control-plane (etcd voting) member.

    The k8s API gives no direct view of etcd membership, so the cheapest
    honest proxy is: every node already carrying the control-plane/etcd role
    must be Ready. Joining a new member while the cluster is already degraded
    is how "one node down" becomes "quorum lost".
    """
    degraded = []
    for node in api.list_node().items:
        labels = node.metadata.labels or {}
        is_cp = ("node-role.kubernetes.io/control-plane" in labels
                 or "node-role.kubernetes.io/etcd" in labels)
        if not is_cp:
            continue
        ready = next(
            (c.status for c in (node.status.conditions or []) if c.type == "Ready"),
            "Unknown",
        )
        if ready != "True":
            degraded.append(f"{node.metadata.name}=Ready:{ready}")

    if degraded:
        print(f"Control plane degraded ({', '.join(degraded)}) — "
              "deferring server join until it recovers.")
        return False
    return True


def parse_workers(cm):
    workers = []
    if not cm.data or not cm.data.get("workers"):
        return workers
    for line in cm.data["workers"].splitlines():
        parts = line.strip().split()
        if len(parts) >= 3:
            workers.append({
                "mac": parts[0],
                "hostname": parts[1],
                "ip": parts[2]
            })
    return workers


def node_is_ready(api, hostname):
    """Is there already a Ready node with this name in the cluster?

    This is the authoritative answer to "is this a working member", and it is
    checked before the SSH probe below. Without it, a node that happens to be
    mid-reboot when the resync fires looks unprovisioned over SSH and would be
    reinstalled underneath itself.
    """
    try:
        node = api.read_node(name=hostname)
    except Exception:
        return False
    for condition in (node.status.conditions or []):
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def is_provisioned(ip, role):
    unit = "k3s" if role == "server" else "k3s-agent"
    try:
        key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
        client_ssh = paramiko.SSHClient()
        client_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client_ssh.connect(ip, username=SSH_USER, pkey=key, timeout=10)
        _, stdout, _ = client_ssh.exec_command(
            f"systemctl is-active {unit} 2>/dev/null || echo inactive"
        )
        result = stdout.read().decode().strip()
        client_ssh.close()
        return result == "active"
    except Exception as e:
        print(f"SSH check failed for {ip}: {e}")
        return False


def warn_if_stale_node_record(api, ssh, hostname):
    """Detect a re-flashed board whose server-side identity is now stale.

    k3s pairs a hostname with a random node password: the machine keeps it in
    /etc/rancher/node/password, the server keeps a hash in the Secret
    <hostname>.node-password.k3s. Re-flashing destroys the machine's half; the
    server's half survives, so registration is refused with "Node password
    rejected" and k3s-agent sits in "activating" forever.

    Deliberately log-only. Clearing the record means overriding k3s's
    anti-hostname-squatting check — that is a deliberate operator action, not
    a standing permission for an unattended pod, so this only tells you the
    exact command to run.
    """
    try:
        api.read_node(name=hostname)
    except Exception:
        return  # no node object for this name — nothing stale

    _, stdout, _ = ssh.exec_command(
        "test -f /etc/rancher/node/password && echo yes || echo no"
    )
    if stdout.read().decode().strip() != "no":
        return  # machine still holds its half of the pair — consistent

    print(
        f"WARNING {hostname}: the cluster has a node record, but this machine "
        f"has no node identity — it looks re-flashed. k3s will install, then "
        f"registration will be REJECTED until the stale record is cleared:\n"
        f"    kubectl delete node {hostname}\n"
        f"    (the node-password Secret is removed with it)\n"
        f"k3s-agent retries on its own, so the node joins once that is done."
    )


def provision(api, worker, server_url, token, role):
    """Provision one node. Returns True only if k3s installed successfully.

    The caller relies on this: a failed server join must not be mistaken for a
    successful one, or an unreachable host consumes the one-join-per-cycle slot.
    """
    ip = worker["ip"]
    hostname = worker["hostname"]
    print(f"Provisioning {hostname} ({ip}) as {role}...")

    try:
        key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=SSH_USER, pkey=key, timeout=10)

        # Pre-flight: surface a stale node record before doing the work, so
        # the reason for a later registration failure is already in the log.
        warn_if_stale_node_record(api, ssh, hostname)

        # 0. Sync time — critical for TLS cert validation.
        #    Seed from this pod's clock so the join works right now, then hand
        #    the node over to NTP. Seeding alone is what the old code did, and
        #    it only holds until the next reboot: these Pis have no
        #    battery-backed RTC, so systemd restores a stale saved timestamp
        #    and the node silently falls out of the cluster's cert validity
        #    window with no way back.
        print(f"Syncing time on {hostname}...")
        # UTC explicitly on both sides. `date -s` interprets a bare timestamp
        # in the TARGET's local timezone, but this pod's clock is UTC — so
        # sending a UTC value unqualified landed every node off by its UTC
        # offset (12h on Pacific/Auckland). The node then failed TLS
        # validation and sat in "activating" forever.
        # set-ntp false first because systemd refuses manual time changes
        # while it is managing the clock; the NTP block below re-enables it.
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _, stdout, stderr = ssh.exec_command(
            f"sudo timedatectl set-ntp false && "
            f"sudo date -u -s '{now}' && "
            f"sudo hwclock -w 2>/dev/null || true"
        )
        _ = stdout.read()
        _ = stderr.read()
        stdout.channel.recv_exit_status()

        if NTP_SERVER:
            _, stdout, stderr = ssh.exec_command(
                f"sudo mkdir -p /etc/systemd/timesyncd.conf.d && "
                f"printf '[Time]\\nNTP={NTP_SERVER}\\n' "
                f"| sudo tee /etc/systemd/timesyncd.conf.d/10-cluster-ntp.conf >/dev/null && "
                f"sudo systemctl enable systemd-timesyncd && "
                f"sudo timedatectl set-ntp true"
            )
            _ = stdout.read()
            ntp_err = stderr.read().decode().strip()
            if stdout.channel.recv_exit_status() == 0:
                print(f"Time synced on {hostname}, NTP -> {NTP_SERVER}")
            else:
                print(f"Time seeded on {hostname}, but NTP setup failed: {ntp_err}")
        else:
            print(f"Time seeded on {hostname} (NTP_SERVER unset — clock will "
                  "drift after reboot)")

        # 1. Transfer artifacts safely into /tmp via SFTP
        print(f"Copying k3s artifacts to {hostname}...")
        sftp = ssh.open_sftp()
        sftp.put(K3S_BINARY_PATH, K3S_BINARY_TMP)
        sftp.put(K3S_INSTALL_SCRIPT_PATH, K3S_INSTALL_SCRIPT_DEST)
        sftp.put(K3S_AIRGAP_IMAGES_PATH, K3S_AIRGAP_IMAGES_DEST)
        sftp.close()

        # 2. Move artifacts into system locations atomically
        print("Moving artifacts into system locations...")
        _, stdout, stderr = ssh.exec_command(
            f"sudo mkdir -p {K3S_IMAGES_DIR} && "
            f"sudo mkdir -p /usr/local/bin && "
            f"sudo mv {K3S_BINARY_TMP} {K3S_BINARY_FINAL} && "
            f"sudo chmod +x {K3S_BINARY_FINAL} && "
            f"sudo chmod +x {K3S_INSTALL_SCRIPT_DEST} && "
            f"sudo mv {K3S_AIRGAP_IMAGES_DEST} "
            f"{K3S_IMAGES_DIR}/k3s-airgap-images-arm64.tar.zst"
        )
        _ = stdout.read().decode().strip()
        setup_err = stderr.read().decode().strip()
        setup_code = stdout.channel.recv_exit_status()

        if setup_code != 0:
            raise Exception(f"System preparation failed with exit code {setup_code}: {setup_err}")

        # 3. Run install script — role decides whether this joins as a
        #    control-plane member or a worker. The server form mirrors
        #    roles/k3s_server (server + --tls-san + --node-ip).
        print(f"Installing k3s {role} on {hostname}...")
        if role == "server":
            install_cmd = (
                f"sudo INSTALL_K3S_SKIP_DOWNLOAD=true "
                f"K3S_URL={server_url} "
                f"K3S_TOKEN={token} "
                f"sh {K3S_INSTALL_SCRIPT_DEST} server "
                f"--tls-san {KUBE_VIP_VIP} "
                f"--node-ip {ip}"
            )
        else:
            install_cmd = (
                f"sudo INSTALL_K3S_SKIP_DOWNLOAD=true "
                f"K3S_URL={server_url} "
                f"K3S_TOKEN={token} "
                f"sh {K3S_INSTALL_SCRIPT_DEST}"
            )

        _, stdout, stderr = ssh.exec_command(install_cmd)
        _ = stdout.read().decode().strip()
        install_err = stderr.read().decode().strip()
        exit_code = stdout.channel.recv_exit_status()

        if exit_code == 0:
            print(f"Successfully provisioned {hostname} as {role}")
            # The k3s binary was already moved into place by step 2, so only
            # the install-script wrapper needs cleaning up.
            ssh.exec_command(f"rm -f {K3S_INSTALL_SCRIPT_DEST}")
            ssh.close()
            return True

        print(f"Failed to provision {hostname} (exit {exit_code}): {install_err}")
        ssh.close()
        return False

    except Exception as e:
        # Deliberately references no locals from the try block — a failure
        # during connect/SFTP happens before install_err/setup_err exist, and
        # touching them here would raise NameError and mask the real error.
        print(f"Provisioning failed for {hostname}: {type(e).__name__}: {e}")
        return False


def process_workers(api, cm, server_url, token):
    # At most one control-plane join per cycle — but a FAILED attempt must not
    # consume that slot, and must not stop agents being processed. This used to
    # `return` unconditionally after any server attempt, so a single
    # unreachable host with a server-shaped name (a Mac Mini called
    # "mac-mini-server") abandoned the whole registry on every cycle and no
    # agent was ever provisioned again.
    server_joined = False

    for worker in parse_workers(cm):
        hostname = worker["hostname"]

        try:
            role = get_role(hostname)
        except ValueError as e:
            print(f"Skipping {hostname} ({worker['ip']}): {e}")
            continue

        # Cluster state first (cheap, authoritative), SSH only as a fallback
        # for "is there even k3s on this box" — e.g. a re-flashed board, which
        # keeps its MAC so it never looks new to lease-watcher.
        if node_is_ready(api, hostname):
            continue
        if is_provisioned(worker["ip"], role):
            continue

        if role == "server":
            if server_joined:
                continue
            if not KUBE_VIP_VIP:
                print(f"Skipping server join for {hostname}: KUBE_VIP_VIP is "
                      "unset, so --tls-san would be missing.")
                continue
            if not control_plane_is_healthy(api):
                continue
            if provision(api, worker, server_url, token, role):
                # A real member joined. It has to register and go Ready before
                # control_plane_is_healthy() can answer meaningfully about
                # adding another, so no further server joins this cycle.
                server_joined = True
                print("Server joined — deferring further server joins "
                      "so etcd settles.")
            continue

        provision(api, worker, server_url, token, role)


def reconcile(api, server_url, token):
    """Level-triggered sweep: bring every registry entry to desired state.

    Runs on a timer rather than only at startup, because plenty of drift
    produces no ConfigMap event at all — a re-flashed board (same MAC, so
    lease-watcher correctly sees nothing new), a failed provision during a pod
    restart, a manual k3s-uninstall, a dead disk. The watch below is only a
    latency optimisation on top of this.
    """
    print("Reconciling existing workers...")
    try:
        cm = api.read_namespaced_config_map(
            name=WORKER_CONFIGMAP,
            namespace=NAMESPACE
        )
        process_workers(api, cm, server_url, token)
    except Exception as e:
        print(f"Reconciliation error: {e}")
    print("Reconciliation complete.")


def main():
    print("Starting ansible-runner...")
    api = load_k8s_client()

    token = get_join_token(api)
    server_url = get_control_plane_url(api)
    print(f"Control plane: {server_url}")
    print(f"kube-vip VIP: {KUBE_VIP_VIP or '(unset — server joins disabled)'}")

    print(f"Watching worker-registry (resync every {RESYNC_INTERVAL}s)...")
    w = watch.Watch()
    while True:
        try:
            # Level-triggered: sweep everything, then watch for changes until
            # the stream times out and drops us back here. New hardware is
            # picked up in seconds by the watch; anything that drifted without
            # producing an event is caught by the next sweep.
            reconcile(api, server_url, token)
            for event in w.stream(
                api.list_namespaced_config_map,
                namespace=NAMESPACE,
                field_selector=f"metadata.name={WORKER_CONFIGMAP}",
                timeout_seconds=RESYNC_INTERVAL,
            ):
                if event["type"] in ["ADDED", "MODIFIED"]:
                    process_workers(api, event["object"], server_url, token)
        except Exception as e:
            # A 401 here means the projected ServiceAccount token rotated and
            # the cached credential went stale. Rebuild the client instead of
            # spinning on Unauthorized forever, and back off so a persistent
            # failure doesn't become a hot loop.
            print(f"Watch stream disconnected ({e}), reconnecting...")
            time.sleep(5)
            try:
                api = load_k8s_client()
            except Exception as reload_err:
                print(f"Client reload failed: {reload_err}")


if __name__ == "__main__":
    main()