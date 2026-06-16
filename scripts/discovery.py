#!/usr/bin/env python3

import os
import re
import subprocess
import sys

import paramiko
import yaml

GROUP_VARS_FILE = "inventories/production/group_vars/all.yml"

SCAN_RANGE_START = 200
SCAN_RANGE_END = 254

SSH_TIMEOUT = 10

INVENTORY_FILE = "inventories/production/hosts.yml"

SERVER_KEYWORDS = ["server", "master", "control"]
WORKER_KEYWORDS = ["worker", "agent", "node"]


def load_group_vars():
    with open(GROUP_VARS_FILE, "r") as f:
        return yaml.safe_load(f)


def network_prefix(group_vars):
    return group_vars["bootstrap_interface_ip"].rsplit(".", 1)[0]


def scan_subnet(prefix):
    target = f"{prefix}.{SCAN_RANGE_START}-{SCAN_RANGE_END}"
    result = subprocess.run(
        ["nmap", "-sn", target],
        capture_output=True,
        text=True,
        timeout=120,
    )

    devices = []
    current_ip = None
    for line in result.stdout.splitlines():
        ip_match = re.match(r"Nmap scan report for (?:\S+ )?\(?([\d.]+)\)?", line)
        if ip_match:
            current_ip = ip_match.group(1)
            continue
        mac_match = re.match(r"MAC Address: ([0-9A-Fa-f:]+)", line)
        if mac_match and current_ip:
            devices.append({"ip": current_ip, "mac": mac_match.group(1).lower()})
            current_ip = None

    return devices


def known_macs(inventory):
    macs = set()
    for group in inventory.get("all", {}).get("children", {}).values():
        for host in group.get("hosts", {}).values():
            mac = host.get("mac_address")
            if mac:
                macs.add(mac.lower())
    return macs


def get_hostname(ip, ssh_key_path, ssh_user):
    try:
        key = paramiko.Ed25519Key.from_private_key_file(os.path.expanduser(ssh_key_path))
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


def load_inventory():
    try:
        with open(INVENTORY_FILE, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def merge_inventory(inventory, nodes, ssh_user):
    inventory.setdefault("all", {}).setdefault("children", {})
    inventory["all"]["children"].setdefault("server", {"hosts": {}})
    inventory["all"]["children"].setdefault("workers", {"hosts": {}})

    for node in nodes:
        role = determine_role(node["hostname"])
        inventory["all"]["children"][role]["hosts"][node["hostname"]] = {
            "ansible_host": node["ip"],
            "ansible_user": ssh_user,
            "mac_address": node["mac"],
        }
        print(f"  {node['hostname']} ({node['ip']}) -> {role}")

    return inventory


def main():
    group_vars = load_group_vars()
    prefix = network_prefix(group_vars)
    ssh_key_path = group_vars["ansible_ssh_private_key_file"]
    ssh_user = group_vars["ansible_user"]

    print(f"Scanning {prefix}.{SCAN_RANGE_START}-{SCAN_RANGE_END}...")
    devices = scan_subnet(prefix)

    if not devices:
        print("No devices found on the subnet.")
        return

    inventory = load_inventory()
    existing_macs = known_macs(inventory)

    new_devices = [d for d in devices if d["mac"] not in existing_macs]
    if not new_devices:
        print("No new nodes found — inventory already up to date.")
        return

    print(f"Found {len(new_devices)} new device(s). Identifying...")
    nodes = []
    for device in new_devices:
        hostname = get_hostname(device["ip"], ssh_key_path, ssh_user)
        if hostname:
            nodes.append({"hostname": hostname, "ip": device["ip"], "mac": device["mac"]})

    if not nodes:
        print("New devices found but none were reachable over SSH yet.")
        return

    print("New nodes:")
    inventory = merge_inventory(inventory, nodes, ssh_user)

    os.makedirs(os.path.dirname(INVENTORY_FILE), exist_ok=True)
    with open(INVENTORY_FILE, "w") as f:
        yaml.dump(inventory, f, default_flow_style=False)

    print(f"Inventory updated: {INVENTORY_FILE}")


if __name__ == "__main__":
    main()
