#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
REGION=${REGION:-us-central1}
SERVICE=${SERVICE:-audiobooks-api}
REPO=${REPO:-audiobooks}
SHEET_ID=${SHEET_ID:-}

if [[ -z "$PROJECT_ID" ]]; then
  echo "GCP project is not set. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

if [[ -z "$SHEET_ID" ]]; then
  echo "SHEET_ID is required. Export SHEET_ID env var before running." >&2
  exit 1
fi

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:latest"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

# Ensure repo exists (idempotent)
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="AudioBooks API" || true

docker build -t "$IMAGE" .
docker push "$IMAGE"

gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --set-env-vars=ENVIRONMENT=production,LOG_LEVEL=INFO,SHEET_ID="$SHEET_ID"

echo "Deployed: $(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"

