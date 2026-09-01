# Gitea's own CI runner, built into the cluster the same way every other
# platform component here is (see custom_images/ansible-runner,
# custom_images/lease-watcher) — a small purpose-built image, not a vendored
# upstream chart, because Gitea's official runner chart was spun out of the
# main gitea/gitea chart into gitea/helm-actions (see
# manifests/gitea/gitea/templates/gitea/check-actions-not-present.yaml,
# which deliberately fails the render if you try the old way) and vendoring
# a second whole Helm chart for one binary + a couple of packages is more
# machinery than this needs.
#
# NAMING NOTE: as of April 2026 upstream renamed this project from
# "act_runner" to "Gitea Runner" (repo moved gitea/act_runner -> gitea/runner,
# binary is now `gitea-runner`, versioning reset to v1.0.0+, currently
# v3.3.x). Everything you'll find by searching still says "act_runner" —
# that's the same project, just pre-rename. Don't mix a downloaded
# act_runner binary with this Dockerfile's setup; the CLI is compatible but
# the binary name below matters for entrypoint.sh.
FROM debian:bookworm-slim

# git: workflow checkout step. buildah: daemonless image builds — see the
# runner Deployment's securityContext for why this, not Docker-in-Docker or
# Kaniko (Kaniko was archived by Google in 2025, no more CVE fixes — not
# something to build a security-focused capstone's CI on). uidmap: provides
# newuidmap/newgidmap, which rootless buildah needs even with the vfs storage
# driver. ca-certificates/curl: fetching the runner binary below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git buildah uidmap ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pinned, not :latest — this is a platform component ArgoCD does NOT
# reconcile (see manifests/gitea-runner/deployment.yml), so an unpinned tag
# would only ever change silently on a rebuild, with no record of what
# changed. Bump this deliberately; check
# https://gitea.com/gitea/runner/releases first.
ARG GITEA_RUNNER_VERSION=3.3.2
RUN curl -fL -o /usr/local/bin/gitea-runner \
        "https://gitea.com/gitea/runner/releases/download/v${GITEA_RUNNER_VERSION}/gitea-runner-${GITEA_RUNNER_VERSION}-linux-arm64" \
    && chmod +x /usr/local/bin/gitea-runner

# Rootless buildah needs a real subuid/subgid range for whichever user it
# runs as — same requirement as rootless Podman, same reason the ansible
# repo's own bootstrap laptop needed this worked out for cross-arch Podman
# builds during preflight.yml.
RUN useradd -u 1000 -m -d /home/build build \
    && echo "build:100000:65536" > /etc/subuid \
    && echo "build:100000:65536" > /etc/subgid \
    && mkdir -p /data && chown build:build /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER build
WORKDIR /home/build
ENTRYPOINT ["/entrypoint.sh"]
