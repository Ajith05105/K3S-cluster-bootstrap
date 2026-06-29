#!/usr/bin/env python3

import base64
import datetime
import paramiko
from kubernetes import client, config, watch

WORKER_CONFIGMAP = "worker-registry"
NAMESPACE = "kube-system"
SSH_USER = "pi"
SSH_KEY_PATH = "/root/.ssh/ansible_runner_key"

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


def is_provisioned(ip):
    try:
        key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
        client_ssh = paramiko.SSHClient()
        client_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client_ssh.connect(ip, username=SSH_USER, pkey=key, timeout=10)
        _, stdout, _ = client_ssh.exec_command(
            "systemctl is-active k3s-agent 2>/dev/null || echo inactive"
        )
        result = stdout.read().decode().strip()
        client_ssh.close()
        return result == "active"
    except Exception as e:
        print(f"SSH check failed for {ip}: {e}")
        return False


def provision(worker, server_url, token):
    ip = worker["ip"]
    hostname = worker["hostname"]
    print(f"Provisioning {hostname} ({ip})...")

    try:
        key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=SSH_USER, pkey=key, timeout=10)

        # 0. Sync time — critical for TLS cert validation
        print(f"Syncing time on {hostname}...")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _, stdout, stderr = ssh.exec_command(
            f"sudo timedatectl set-ntp false && "
            f"sudo date -s '{now}' && "
            f"sudo hwclock -w"
        )
        _ = stdout.read()
        _ = stderr.read()
        stdout.channel.recv_exit_status()
        print(f"Time synced on {hostname}")

        # 1. Transfer artifacts safely into /tmp via SFTP
        print(f"Copying k3s artifacts to {hostname}...")
        sftp = ssh.open_sftp()
        sftp.put(K3S_BINARY_PATH, K3S_BINARY_TMP)
        sftp.put(K3S_INSTALL_SCRIPT_PATH, K3S_INSTALL_SCRIPT_DEST)
        sftp.put(K3S_AIRGAP_IMAGES_PATH, K3S_AIRGAP_IMAGES_DEST)
        sftp.close()

        # 2. Move artifacts into system locations atomically
        print("Moving artifacts into system locations...")
        # FIX 1: Unpack all 3 values (including stdin) to prevent ValueError unpacking crash
        _, stdout, stderr = ssh.exec_command(
            f"sudo mkdir -p {K3S_IMAGES_DIR} && "
            f"sudo mkdir -p /usr/local/bin && "
            f"sudo mv {K3S_BINARY_TMP} {K3S_BINARY_FINAL} && "
            f"sudo chmod +x {K3S_BINARY_FINAL} && "
            f"sudo chmod +x {K3S_INSTALL_SCRIPT_DEST} && "
            f"sudo mv {K3S_AIRGAP_IMAGES_DEST} "
            f"{K3S_IMAGES_DIR}/k3s-airgap-images-arm64.tar.zst"
        )
        # FIX 2: Capture errors in case the chained file migrations fail
        _ = stdout.read().decode().strip()
        setup_err = stderr.read().decode().strip()
        setup_code = stdout.channel.recv_exit_status()

        if setup_code != 0:
            raise Exception(f"System preparation failed with exit code {setup_code}: {setup_err}")

        # 3. Run install script — sets up systemd service, returns clean exit code
        print(f"Installing k3s agent on {hostname}...")
        install_cmd = (
            f"sudo INSTALL_K3S_SKIP_DOWNLOAD=true "
            f"K3S_URL={server_url} "
            f"K3S_TOKEN={token} "
            f"sh {K3S_INSTALL_SCRIPT_DEST}"
        )
        _, stdout, stderr = ssh.exec_command(install_cmd)
        install_out = stdout.read().decode().strip()
        install_err = stderr.read().decode().strip()
        exit_code = stdout.channel.recv_exit_status()

        if exit_code == 0:
            print(f"Successfully provisioned {hostname}")
            # FIX 3: Since k3s binary was already moved by step 2, just wipe the unneeded installation script wrapper
            ssh.exec_command(f"rm -f {K3S_INSTALL_SCRIPT_DEST}")
        else:
            print(f"Failed to provision {hostname}: {stderr.read().decode()}")

        ssh.close()

    except Exception as e:
        print(f"Provisioning failed for {hostname}: {e} | Context: {install_err}")


def reconcile(api, server_url, token):
    print("Reconciling existing workers...")
    try:
        cm = api.read_namespaced_config_map(
            name=WORKER_CONFIGMAP,
            namespace=NAMESPACE
        )
        for worker in parse_workers(cm):
            if not is_provisioned(worker["ip"]):
                provision(worker, server_url, token)
    except Exception as e:
        print(f"Reconciliation error on startup: {e}")
    print("Reconciliation complete.")


def main():
    print("Starting ansible-runner...")
    api = load_k8s_client()

    token = get_join_token(api)
    server_url = get_control_plane_url(api)
    print(f"Control plane: {server_url}")

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
                    cm = event["object"]
                    for worker in parse_workers(cm):
                        if not is_provisioned(worker["ip"]):
                            provision(worker, server_url, token)
        except Exception as e:
            print(f"Watch stream disconnected ({e}), reconnecting...")


if __name__ == "__main__":
    main()
