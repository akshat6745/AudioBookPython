#!/usr/bin/env bash
# Deploy the committed code (git HEAD) to the e2-micro VM.
#
# Ships `git archive HEAD`, so only tracked files reach the server — no .env,
# service-account JSON, .venv or EPUBs, and uncommitted changes are NOT
# deployed. Secrets come from your local .env, copied separately.
#
# Usage: scripts/gce/deploy.sh   (optionally DOMAIN=api.example.com)
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$(git rev-parse --show-toplevel)"

[ -f .env ] || { echo "No .env in repo root — the server needs it for secrets." >&2; exit 1; }

IP=$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
# No domain yet? sslip.io resolves <ip>.sslip.io to <ip>, which is enough for
# Caddy to get a real Let's Encrypt certificate.
DOMAIN=${DOMAIN:-$IP.sslip.io}

mkdir -p "$(dirname "$SSH_KEY")"
ssh_vm() { gcloud compute ssh "$VM_NAME" --zone="$ZONE" --ssh-key-file="$SSH_KEY" --command="$1"; }

echo "Waiting for Docker on the VM (installed by the startup script on first boot)..."
ssh_vm 'for i in $(seq 60); do command -v docker >/dev/null && exit 0; sleep 5; done; echo "Docker not installed after 5 min — check: sudo journalctl -u google-startup-scripts" >&2; exit 1'

gcloud compute scp .env "$VM_NAME":audiobook.env --zone="$ZONE" --ssh-key-file="$SSH_KEY"

git archive --format=tar HEAD | gcloud compute ssh "$VM_NAME" --zone="$ZONE" --ssh-key-file="$SSH_KEY" \
  --command='rm -rf ~/release && mkdir ~/release && tar -x -C ~/release && mv ~/audiobook.env ~/release/.env'

ssh_vm "cd ~/release && sudo DOMAIN='$DOMAIN' docker compose -f docker-compose.prod.yml up -d --build --remove-orphans && sudo docker image prune -f"

echo "Deployed $(git rev-parse --short HEAD). Checking https://$DOMAIN/health (first run waits for the TLS certificate)..."
for _ in $(seq 12); do
  curl -fsS --max-time 10 "https://$DOMAIN/health" && echo && exit 0
  sleep 10
done
echo "Health check failed — inspect with: CLOUDSDK_CONFIG=$CLOUDSDK_CONFIG gcloud compute ssh $VM_NAME --zone=$ZONE --ssh-key-file=$SSH_KEY --command='cd ~/release && sudo docker compose -f docker-compose.prod.yml logs --tail=100'" >&2
exit 1
