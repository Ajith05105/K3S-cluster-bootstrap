#!/usr/bin/env python3

import time
from kubernetes import client, config

LEASE_FILE = "/var/lib/dnsmasq/dnsmasq.leases"
CP_CONFIGMAP = "control-plane-registry"
WORKER_CONFIGMAP = "worker-registry"
NAMESPACE = "kube-system"
POLL_INTERVAL = 5


def load_k8s_client():
    config.load_incluster_config()
    return client.CoreV1Api()


def get_all_known_macs(api):
    """Reads both ConfigMaps to track every MAC address currently in the cluster."""
    known_macs = set()
    worker_cm = None  # Fix 1: Initialize to prevent unbound variable crash

    # 1. Parse Control Plane Static MACs
    try:
        cp_cm = api.read_namespaced_config_map(name=CP_CONFIGMAP, namespace=NAMESPACE)
        if cp_cm.data and "nodes" in cp_cm.data:
            for line in cp_cm.data["nodes"].splitlines():
                parts = line.strip().split()
                if parts:
                    known_macs.add(parts[0].lower())
    except Exception as e:
        print(f"Warning reading control plane registry: {e}")

    # 2. Parse Worker Dynamic MACs
    try:
        worker_cm = api.read_namespaced_config_map(name=WORKER_CONFIGMAP, namespace=NAMESPACE)
        if worker_cm.data and "workers" in worker_cm.data:
            for line in worker_cm.data["workers"].splitlines():
                parts = line.strip().split()
                if parts:
                    known_macs.add(parts[0].lower())
    except Exception as e:
        print(f"Warning reading worker registry: {e}")

    return worker_cm, known_macs


def read_leases(lease_file):
    leases = []
    try:
        with open(lease_file, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3:
                    leases.append({
                        "mac": parts[1].lower(),
                        "ip": parts[2],
                        "hostname": parts[3] if len(parts) > 3 else "unknown"
                    })
    except FileNotFoundError:
        pass
    return leases


def main():
    print("Starting lease watcher...")
    api = load_k8s_client()

    while True:
        try:
            # 1. Get the current cluster state across both ConfigMaps
            worker_cm, known_macs = get_all_known_macs(api)
            leases = read_leases(LEASE_FILE)

            new_entries = []
            
            # 2. Find ALL new leases in this cycle
            for lease in leases:
                if lease["mac"] not in known_macs:
                    print(f"New node detected: {lease['mac']} → {lease['ip']}")
                    entry = f"{lease['mac']} {lease['hostname']} {lease['ip']}\n"
                    new_entries.append(entry)
                    known_macs.add(lease["mac"])

            # 3. If there are changes, patch the worker registry ONLY
            # Fix 2: Guard against patching if worker_cm couldn't be loaded
            if new_entries and worker_cm is not None:
                current_workers = worker_cm.data.get("workers", "") if worker_cm.data else ""
                
                if current_workers is None:
                    current_workers = ""
                
                if current_workers and not current_workers.endswith("\n"):
                    current_workers += "\n"
                
                updated_workers = current_workers + "".join(new_entries)
                
                patch_body = {
                    "data": {
                        "workers": updated_workers
                    }
                }

                api.patch_namespaced_config_map(
                    name=WORKER_CONFIGMAP,
                    namespace=NAMESPACE,
                    body=patch_body
                )
                print(f"Successfully updated worker registry with {len(new_entries)} new worker(s).")

        except Exception as e:
            print(f"Error in execution loop: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()