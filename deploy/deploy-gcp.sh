#!/usr/bin/env bash
# Deploy TermGuard to Cloud Run.
#
# Everything that differs between local and GCP is an environment variable, so this
# script builds the same image `make demo` runs against and points it at managed
# services. Nothing in termguard/ changes.
#
#   ./deploy/deploy-gcp.sh PROJECT_ID [REGION]
#
# Prerequisites (one-time, see deploy/README.md for the commands):
#   - a GCS bucket for the object store
#   - a Cloud SQL Postgres instance and database
#   - a service account with roles/storage.objectAdmin on the bucket and
#     roles/cloudsql.client on the instance
#   - the Anthropic API key in Secret Manager, if the live LLM step is wanted

set -euo pipefail

PROJECT="${1:?usage: deploy-gcp.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"

SERVICE="termguard"
BUCKET="${TERMGUARD_BUCKET:-${PROJECT}-termguard-docs}"
INSTANCE="${TERMGUARD_SQL_INSTANCE:-termguard-pg}"
DB_NAME="${TERMGUARD_DB_NAME:-termguard}"
DB_USER="${TERMGUARD_DB_USER:-termguard}"
SERVICE_ACCOUNT="${TERMGUARD_SA:-termguard-run@${PROJECT}.iam.gserviceaccount.com}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/termguard/${SERVICE}"

CONNECTION_NAME="${PROJECT}:${REGION}:${INSTANCE}"

echo "==> building ${IMAGE}"
# Built from the repo root so the Dockerfile can copy both termguard/ and web/.
gcloud builds submit \
  --project "${PROJECT}" \
  --tag "${IMAGE}" \
  --ignore-file .dockerignore \
  .

echo "==> deploying ${SERVICE} to ${REGION}"
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --add-cloudsql-instances "${CONNECTION_NAME}" \
  --set-env-vars "TERMGUARD_STORAGE=gcs" \
  --set-env-vars "TERMGUARD_GCS_BUCKET=${BUCKET}" \
  --set-env-vars "TERMGUARD_DB_URL=postgresql+psycopg://${DB_USER}@/${DB_NAME}?host=/cloudsql/${CONNECTION_NAME}" \
  --set-env-vars "TERMGUARD_OUT_DIR=/tmp/termguard-out" \
  --set-env-vars "ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-claude-opus-5}" \
  --set-secrets "ANTHROPIC_API_KEY=anthropic-api-key:latest" \
  --cpu 2 --memory 2Gi --timeout 900 \
  --min-instances 0 --max-instances 4 \
  --no-allow-unauthenticated

echo
echo "deployed. URL:"
gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" \
  --format 'value(status.url)'
echo
echo "Access is authenticated-only (--no-allow-unauthenticated). TermGuard has no auth of"
echo "its own by design, so IAM is what stands in front of it. Grant access with:"
echo "  gcloud run services add-iam-policy-binding ${SERVICE} --region ${REGION} \\"
echo "    --member user:someone@example.com --role roles/run.invoker"
