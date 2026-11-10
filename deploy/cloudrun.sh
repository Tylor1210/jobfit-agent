#!/usr/bin/env bash
# Build the Docker image with Cloud Build and deploy it to Cloud Run. Run from the repo root:
#   bash deploy/cloudrun.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.env

# Flags, and why:
#   --allow-unauthenticated   Cloudflare must reach it; the app itself rejects any request without
#                             a valid Cloudflare Access token (REQUIRE_ACCESS=1 fails closed).
#   --no-cpu-throttling       tailoring keeps running after the request returns (background task);
#                             with throttling, Cloud Run would freeze it mid-draft.
#   --max-instances 1         in-flight task status lives in memory; one instance keeps it simple.
#   --min-instances 0         scale to zero when idle (first visit after idle takes ~10-20 s).
#   --memory 2Gi              Chromium (PDFs) + the embedding model.
gcloud run deploy "$SERVICE" \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --source . \
  --allow-unauthenticated \
  --memory 2Gi --cpu 1 \
  --min-instances 0 --max-instances 1 --concurrency 20 \
  --no-cpu-throttling \
  --timeout 900 \
  --set-env-vars "REQUIRE_ACCESS=1,CF_ACCESS_TEAM_DOMAIN=${CF_ACCESS_TEAM_DOMAIN},LANGFUSE_BASE_URL=${LANGFUSE_BASE_URL}" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,GROQ_API_KEY=GROQ_API_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,LANGFUSE_PUBLIC_KEY=LANGFUSE_PUBLIC_KEY:latest,LANGFUSE_SECRET_KEY=LANGFUSE_SECRET_KEY:latest,CF_ACCESS_AUD=CF_ACCESS_AUD:latest"

URL="$(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" --region "$GCP_REGION" --format='value(status.url)')"
echo
echo "Cloud Run URL: $URL"
echo "Expect 403 here (login required):  curl -s -o /dev/null -w '%{http_code}\n' $URL/api/jobs"
echo "Expect 200 here (health is open):  curl -s -o /dev/null -w '%{http_code}\n' $URL/api/health"
echo "Next: put this URL in deploy/worker/wrangler.toml (ORIGIN) and run: cd deploy/worker && npx wrangler deploy"
