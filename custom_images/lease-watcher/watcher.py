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


def read_registry(api, name, key):
    """Read a registry ConfigMap into {mac: (hostname, ip)}.

    Both registries use the same line format: "<mac> <hostname> <ip>". Note
    this is NOT the dnsmasq lease order — see read_leases().

    Fails CLOSED: a read error raises rather than returning a partial map. The
    old behaviour was to warn and carry on, which meant a transient API error
    produced an empty control-plane set, so every server looked like a brand
    new node and got queued for agent provisioning. That is exactly how a
    control-plane MAC ended up in worker-registry pointing at a dead IP.
    """
    cm = api.read_namespaced_config_map(name=name, namespace=NAMESPACE)
    entries = {}
    if cm.data and cm.data.get(key):
        for line in cm.data[key].splitlines():
            parts = line.strip().split()
            if len(parts) >= 3:
                entries[parts[0].lower()] = (parts[1], parts[2])
    return cm, entries


def read_leases(lease_file):
    """Parse dnsmasq's lease file: "<expiry> <mac> <ip> <hostname> <clientid>".

    Leases with no usable hostname are dropped. dnsmasq writes "*" when a
    client sends none, and ansible-runner would reject such an entry anyway
    since it can't derive a role from it — so recording it is pure noise.
    """
    leases = []
    try:
        with open(lease_file, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                hostname = parts[3]
                if hostname in ("*", ""):
                    continue
                leases.append({
                    "mac": parts[1].lower(),
                    "ip": parts[2],
                    "hostname": hostname,
                })
    except FileNotFoundError:
        # The lease volume is emptyDir, so this is empty on every pod restart.
        # Returning [] rather than raising is deliberate: an absent lease file
        # must never be read as "these nodes are gone".
        pass
    return leases


def render(entries):
    """Serialise {mac: (hostname, ip)} back to ConfigMap text.

    Sorted so an unchanged fleet always produces byte-identical output —
    otherwise dict ordering alone would rewrite the ConfigMap every cycle, and
    ansible-runner watches this object.
    """
    return "".join(f"{mac} {host} {ip}\n"
                   for mac, (host, ip) in sorted(entries.items()))


def main():
    print("Starting lease watcher...")
    api = load_k8s_client()

    while True:
        try:
            # Control-plane MACs are tracked separately and never written into
            # the worker registry — ansible-runner provisions whatever it finds
            # there, and a server landing in that list means a control-plane
            # node gets rebuilt as an agent.
            _, cp_entries = read_registry(api, CP_CONFIGMAP, "nodes")
            worker_cm, workers = read_registry(api, WORKER_CONFIGMAP, "workers")
            cp_macs = set(cp_entries)

            before = render(workers)

            # Upsert keyed on MAC. A board re-flashed under a new hostname, or
            # one that simply moved to a different IP, overwrites its own row
            # instead of being skipped as "already known" — which is what left
            # a dead agent-2 at .155 in the registry for eighteen days while
            # the same hardware sat at .154 under a new name.
            for lease in read_leases(LEASE_FILE):
                mac = lease["mac"]
                if mac in cp_macs:
                    continue

                desired = (lease["hostname"], lease["ip"])
                current = workers.get(mac)
                if current == desired:
                    continue

                if current is None:
                    print(f"New node detected: {mac} → {desired[1]} ({desired[0]})")
                else:
                    print(f"Updating {mac}: {current[0]} {current[1]} "
                          f"→ {desired[0]} {desired[1]}")
                workers[mac] = desired

            # Entries whose MAC is absent from the lease file are deliberately
            # left alone. The lease volume does not survive a pod restart, so
            # pruning on that basis would wipe the whole registry the moment
            # dnsmasq restarts, and ansible-runner would forget every node.
            after = render(workers)
            if after != before:
                api.patch_namespaced_config_map(
                    name=WORKER_CONFIGMAP,
                    namespace=NAMESPACE,
                    body={"data": {"workers": after}},
                )
                print(f"Worker registry updated ({len(workers)} entries).")

        except Exception as e:
            # Skip the cycle entirely rather than acting on partial state.
            # Also rebuild the client: a persistent 401 here usually means the
            # cached credential went stale, and retrying forever with the same
            # dead client is how this silently stopped working for weeks.
            print(f"Error in execution loop, skipping cycle: {e}")
            try:
                api = load_k8s_client()
            except Exception as reload_err:
                print(f"Client reload failed: {reload_err}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
