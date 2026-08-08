#!/bin/bash
# Usage: ./tenant-creds.sh <tenant-name>
NAME=$1
if [ -z "$NAME" ]; then
  echo "Usage: $0 <tenant-name>"
  exit 1
fi
PASS=$(kubectl get secret "tenant-${NAME}-app-repo" -n argocd -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)
if [ -z "$PASS" ]; then
  echo "No such tenant: $NAME (has sync_tenants.yml been run since they were declared?)"
  exit 1
fi
echo "=================================="
echo " Gitea login for: $NAME"
echo "=================================="
echo "URL:      http://gitea.cluster.local"
echo "Username: $NAME"
echo "Password: $PASS"
echo "Repo:     http://gitea.cluster.local/$NAME/app"
echo "=================================="
