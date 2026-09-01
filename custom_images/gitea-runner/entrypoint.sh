#!/bin/sh
# Registers this runner with Gitea on first boot, then runs it forever.
# Idempotent across pod restarts as long as /data is the same PVC (see
# manifests/gitea-runner/deployment.yml) — re-registering on every restart
# would leave a trail of dead runner entries in Gitea's admin panel, and
# each one would look like a legitimate-but-offline runner rather than the
# same one restarting.
set -eu

DATA_DIR=/data
CONFIG_FILE=/home/build/config.yaml
RUNNER_FILE="${DATA_DIR}/.runner"

# Generate the runner's own default config once, from the binary actually in
# this image, rather than hand-maintaining a copy of its schema here — that
# would silently drift the first time this image's GITEA_RUNNER_VERSION
# bumps and the config format moves on. We only ever touch the couple of
# lines called out below.
if [ ! -f "${CONFIG_FILE}" ]; then
  gitea-runner generate-config > "${CONFIG_FILE}"
fi

# Point the registration-state file at the PVC, not this container's
# writable layer. If this line's exact shape ever stops matching (upstream
# changes config.example.yaml's layout), `gitea-runner generate-config` will
# show you the new key to target here.
sed -i "s#^\(\s*file:\).*#\1 ${RUNNER_FILE}#" "${CONFIG_FILE}"

if [ ! -f "${RUNNER_FILE}" ]; then
  echo "No registration found in ${DATA_DIR} — registering with Gitea at ${GITEA_INSTANCE_URL}..."
  gitea-runner register \
    --no-interactive \
    --instance "${GITEA_INSTANCE_URL}" \
    --token "$(cat /run/secrets/gitea-runner-token/token)" \
    --name "${RUNNER_NAME:-gitea-runner-1}" \
    --labels "arm64:host" \
    --config "${CONFIG_FILE}"
else
  echo "Found existing registration in ${DATA_DIR} (${RUNNER_FILE}), reusing it."
fi

exec gitea-runner daemon --config "${CONFIG_FILE}"
