#!/usr/bin/env bash
# Log in to the Google account used for this deployment, in an isolated
# gcloud config (see env.sh) — your default gcloud login is not affected.
#
# Usage: scripts/gce/login.sh [YOUR_PROJECT_ID]
#   Without a project ID it logs in and lists the account's projects.
set -euo pipefail
source "$(dirname "$0")/env.sh"

if [ -z "$(gcloud auth list --filter=status:ACTIVE --format='value(account)')" ]; then
  gcloud auth login
fi
echo "Logged in as $(gcloud auth list --filter=status:ACTIVE --format='value(account)') (isolated config at $CLOUDSDK_CONFIG)"

if [ $# -eq 0 ]; then
  gcloud projects list
  echo "Re-run with a PROJECT_ID from above: scripts/gce/login.sh PROJECT_ID"
  exit 0
fi
gcloud config set project "$1"
