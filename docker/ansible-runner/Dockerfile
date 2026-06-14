FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && pip install --no-cache-dir ansible \
    && rm -rf /var/lib/apt/lists/*

COPY playbooks/join_node.yml /playbooks/join_node.yml
COPY roles/k3s_prereqs/ /roles/k3s_prereqs/
COPY roles/k3s_common/ /roles/k3s_common/
COPY roles/k3s_agent/ /roles/k3s_agent/
COPY roles/k3s_server/ /roles/k3s_server/
