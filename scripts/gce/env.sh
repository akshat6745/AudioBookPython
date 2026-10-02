# Sourced by the scripts in scripts/gce/. Isolates everything gcloud touches
# for this deployment from your default gcloud setup:
#   CLOUDSDK_CONFIG — separate accounts, credentials and active project
#                     (your ~/.config/gcloud is never read or written)
#   SSH_KEY         — separate SSH key for the VM instead of ~/.ssh/google_compute_engine
export CLOUDSDK_CONFIG=${CLOUDSDK_CONFIG:-$HOME/.config/gcloud-audiobook}
SSH_KEY="$CLOUDSDK_CONFIG/ssh/google_compute_engine"

VM_NAME=${VM_NAME:-audiobook-api}
ZONE=${ZONE:-us-west1-c}
