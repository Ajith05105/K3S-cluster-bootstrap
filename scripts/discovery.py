#!/usr/bin/env python3

import yaml
import sys
import os

LEASE_FILE = "/tmp/dnsmasq.leases"
INVENTORY_FILE = "inventories/production/hosts.yml"


def read_leases(lease_file):
    nodes = {}
    try:
        with open(lease_file, 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 4:
                    mac = parts[1]
                    ip = parts[2]
                    hostname = parts[3]
                    if hostname != "*" and mac not in nodes:
                        nodes[mac] = {"hostname": hostname, "ip": ip, "mac": mac}
    except FileNotFoundError:
        print(f"Lease file not found: {lease_file}")
        sys.exit(1)
    return list(nodes.values())


def generate_inventory(nodes):
    inventory = {
        "all": {
            "children": {
                "server": {
                    "hosts": {}
                }
            }
        }
    }

    for node in nodes:
        inventory["all"]["children"]["server"]["hosts"][node["hostname"]] = {
            "ansible_host": node["ip"],
            "mac_address": node["mac"]
        }
        print(f"  {node['hostname']} ({node['ip']})")

    return inventory


def main():
    print("Reading DHCP leases...")
    nodes = read_leases(LEASE_FILE)

    if not nodes:
        print("No leases found. Are the nodes powered on?")
        sys.exit(1)

    print(f"\nDiscovered {len(nodes)} server node(s):")
    inventory = generate_inventory(nodes)

    os.makedirs(os.path.dirname(INVENTORY_FILE), exist_ok=True)
    with open(INVENTORY_FILE, 'w') as f:
        yaml.dump(inventory, f, default_flow_style=False)

    print(f"\nInventory written to {INVENTORY_FILE}")


if __name__ == "__main__":
    main()
