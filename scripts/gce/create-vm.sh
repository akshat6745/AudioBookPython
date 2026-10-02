#!/usr/bin/env bash
# One-time: create the Always Free e2-micro VM and open HTTP/HTTPS.
#
# Every flag below is what keeps this inside the free tier — changing the
# machine type, region, disk type/size or network tier makes it billable.
#   e2-micro          — the only free machine type
#   us-west1          — free tier is only us-west1 / us-central1 / us-east1
#   pd-standard 30GB  — free disk is 30 GB of *standard* PD (default is balanced = paid)
#   STANDARD tier     — 200 GB/month free egress (Premium tier gets ~1 GB)
#
# Usage: scripts/gce/create-vm.sh   (run scripts/gce/login.sh first)
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
source "$SCRIPT_DIR/env.sh"

gcloud services enable compute.googleapis.com

gcloud compute instances create "$VM_NAME" \
  --zone="$ZONE" \
  --machine-type=e2-micro \
  --network-tier=STANDARD \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard \
  --tags=audiobook-web \
  --metadata-from-file=startup-script="$SCRIPT_DIR/startup.sh"

if ! gcloud compute firewall-rules describe allow-audiobook-web >/dev/null 2>&1; then
  gcloud compute firewall-rules create allow-audiobook-web \
    --allow=tcp:80,tcp:443 \
    --target-tags=audiobook-web \
    --description="HTTP/HTTPS for Caddy on the audiobook VM"
fi

IP=$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
echo "VM created: $VM_NAME ($IP). First boot installs Docker — give it ~3 minutes, then run scripts/gce/deploy.sh"
