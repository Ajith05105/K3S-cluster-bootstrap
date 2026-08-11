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


def provision(worker, server_url, token, role):
    ip = worker["ip"]
    hostname = worker["hostname"]
    print(f"Provisioning {hostname} ({ip}) as {role}...")

    try:
        key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=SSH_USER, pkey=key, timeout=10)

        # 0. Sync time — critical for TLS cert validation.
        #    Seed from this pod's clock so the join works right now, then hand
        #    the node over to NTP. Seeding alone is what the old code did, and
        #    it only holds until the next reboot: these Pis have no
        #    battery-backed RTC, so systemd restores a stale saved timestamp
        #    and the node silently falls out of the cluster's cert validity
        #    window with no way back.
        print(f"Syncing time on {hostname}...")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _, stdout, stderr = ssh.exec_command(
            f"sudo timedatectl set-ntp false && "
            f"sudo date -s '{now}' && "
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
        else:
            print(f"Failed to provision {hostname} (exit {exit_code}): {install_err}")

        ssh.close()

    except Exception as e:
        # Deliberately references no locals from the try block — a failure
        # during connect/SFTP happens before install_err/setup_err exist, and
        # touching them here would raise NameError and mask the real error.
        print(f"Provisioning failed for {hostname}: {type(e).__name__}: {e}")


def process_workers(api, cm, server_url, token):
    for worker in parse_workers(cm):
        hostname = worker["hostname"]

        try:
            role = get_role(hostname)
        except ValueError as e:
            print(f"Skipping {hostname} ({worker['ip']}): {e}")
            continue

        if is_provisioned(worker["ip"], role):
            continue

        if role == "server":
            if not KUBE_VIP_VIP:
                print(f"Skipping server join for {hostname}: KUBE_VIP_VIP is "
                      "unset, so --tls-san would be missing.")
                continue
            if not control_plane_is_healthy(api):
                continue
            provision(worker, server_url, token, role)
            # One control-plane join per cycle. The new member has to register
            # and go Ready before control_plane_is_healthy() can give a
            # meaningful answer about whether it's safe to add another.
            print("Server join attempted — pausing this cycle so etcd settles.")
            return

        provision(worker, server_url, token, role)


def reconcile(api, server_url, token):
    print("Reconciling existing workers...")
    try:
        cm = api.read_namespaced_config_map(
            name=WORKER_CONFIGMAP,
            namespace=NAMESPACE
        )
        process_workers(api, cm, server_url, token)
    except Exception as e:
        print(f"Reconciliation error on startup: {e}")
    print("Reconciliation complete.")


def main():
    print("Starting ansible-runner...")
    api = load_k8s_client()

    token = get_join_token(api)
    server_url = get_control_plane_url(api)
    print(f"Control plane: {server_url}")
    print(f"kube-vip VIP: {KUBE_VIP_VIP or '(unset — server joins disabled)'}")

    # 1. Reconcile on startup — catch nodes added before ansible-runner deployed
    reconcile(api, server_url, token)

    # 2. Watch for new entries — resilient against stream timeouts
    print("Watching worker-registry for new nodes...")
    w = watch.Watch()
    while True:
        try:
            for event in w.stream(
                api.list_namespaced_config_map,
                namespace=NAMESPACE,
                field_selector=f"metadata.name={WORKER_CONFIGMAP}"
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
