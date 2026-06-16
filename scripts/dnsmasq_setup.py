#!/usr/bin/env python3

import platform
import subprocess
import sys
import time

INTERFACE = "en8"
DHCP_RANGE_START = "192.168.1.200"
DHCP_RANGE_END = "192.168.1.254"
SUBNET_MASK = "255.255.255.0"
LEASE_FILE = "/tmp/dnsmasq.leases"

DARWIN_BIN = "/opt/homebrew/sbin/dnsmasq"
DARWIN_CONF = "/usr/local/etc/dnsmasq.conf"
LINUX_BIN = "/usr/sbin/dnsmasq"
LINUX_CONF = "/etc/dnsmasq.conf"


def dnsmasq_paths():
    if platform.system() == "Darwin":
        return DARWIN_BIN, DARWIN_CONF
    return LINUX_BIN, LINUX_CONF


def render_conf():
    lines = [
        "port=0",
        f"interface={INTERFACE}",
        "bind-interfaces",
        f"dhcp-range={DHCP_RANGE_START},{DHCP_RANGE_END},{SUBNET_MASK},infinite",
        f"dhcp-leasefile={LEASE_FILE}",
        "no-resolv",
        "log-dhcp",
        "domain=cluster.local",
        "domain-needed",
        "bogus-priv",
    ]
    return "\n".join(lines) + "\n"


def is_running():
    return subprocess.run(["pgrep", "dnsmasq"], capture_output=True).returncode == 0


def start():
    dnsmasq_bin, conf_path = dnsmasq_paths()

    with open(conf_path, "w") as f:
        f.write(render_conf())

    if is_running():
        print("dnsmasq is already running")
        return

    subprocess.run([dnsmasq_bin, "-C", conf_path], check=True)

    for _ in range(5):
        if is_running():
            print("dnsmasq started")
            return
        time.sleep(2)

    print("dnsmasq did not start", file=sys.stderr)
    sys.exit(1)


def stop():
    if not is_running():
        print("dnsmasq is not running")
        return
    subprocess.run(["pkill", "dnsmasq"], check=True)
    print("dnsmasq stopped")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        start()
    elif action == "stop":
        stop()
    else:
        print(f"Unknown action: {action} (expected 'start' or 'stop')", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
