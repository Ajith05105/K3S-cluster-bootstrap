#!/usr/bin/env python3

import yaml
import sys
import os
import paramiko
from pathlib import Path

LEASE_FILE = "/tmp/dnsmasq.leases"
INVENTORY_FILE = "inventories/production/hosts.yml"
GROUP_VARS_FILE = "inventories/production/group_vars/all.yml"
SSH_TIMEOUT = 10

SERVER_KEYWORDS = ["server", "master", "control"]
WORKER_KEYWORDS = ["worker", "agent", "node"]


def load_group_vars(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)


def read_leases(lease_file):
    leases = []
    try:
        with open(lease_file, 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3:
                    leases.append({
                        "ip": parts[2],
                        "mac": parts[1],
                        "hostname": parts[3] if len(parts) > 3 else "unknown"
                    })
    except FileNotFoundError:
        print(f"Lease file not found: {lease_file}")
        sys.exit(1)
    return leases


def get_hostname(ip, key_path, ssh_user):
    try:
        key = paramiko.Ed25519Key.from_private_key_file(os.path.expanduser(key_path))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(ip, username=ssh_user, pkey=key, timeout=SSH_TIMEOUT)
        _, stdout, _ = client.exec_command("hostname")
        hostname = stdout.read().decode().strip()
        client.close()
        return hostname
    except Exception as e:
        print(f"Failed to SSH into {ip}: {e}")
        return None


def determine_role(hostname):
    hostname_lower = hostname.lower()
    for keyword in SERVER_KEYWORDS:
        if keyword in hostname_lower:
            return "server"
    return "workers"


def generate_inventory(nodes, ssh_user):
    try:
        with open(INVENTORY_FILE, 'r') as f:
            inventory = yaml.safe_load(f) or {}
    except FileNotFoundError:
        inventory = {}

    inventory.setdefault("all", {}).setdefault("children", {})
    inventory["all"]["children"].setdefault("server", {"hosts": {}})
    inventory["all"]["children"].setdefault("workers", {"hosts": {}})

    for node in nodes:
        role = determine_role(node["hostname"])
        inventory["all"]["children"][role]["hosts"][node["hostname"]] = {
            "ansible_host": node["ip"],
            "ansible_user": ssh_user,
            "mac_address": node["mac"]
        }
        print(f"  {node['hostname']} ({node['ip']}) → {role}")

    return inventory


def main():
    group_vars = load_group_vars(GROUP_VARS_FILE)
    key_path = group_vars.get("ansible_ssh_private_key_file")
    if not key_path:
        print("ansible_ssh_private_key_file not set in group_vars/all.yml")
        sys.exit(1)
    ssh_user = os.environ.get("BOOTSTRAP_USER", "pi")

    print("Reading DHCP leases...")
    leases = read_leases(LEASE_FILE)

    if not leases:
        print("No leases found. Are the nodes powered on?")
        sys.exit(1)

    print(f"Found {len(leases)} lease(s). Discovering hostnames...")
    nodes = []
    for lease in leases:
        hostname = get_hostname(lease["ip"], key_path, ssh_user)
        if hostname:
            nodes.append({"hostname": hostname, "ip": lease["ip"], "mac": lease["mac"]})

    if not nodes:
        print("No nodes reachable.")
        sys.exit(1)

    print(f"\nDiscovered nodes:")
    inventory = generate_inventory(nodes, ssh_user)

    os.makedirs(os.path.dirname(INVENTORY_FILE), exist_ok=True)
    with open(INVENTORY_FILE, 'w') as f:
        yaml.dump(inventory, f, default_flow_style=False)

    print(f"\nInventory written to {INVENTORY_FILE}")


if __name__ == "__main__":
    main()
