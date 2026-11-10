#!/usr/bin/env bash
# Copy secrets from .env into Google Secret Manager (creates or adds a new version).
# Run once, and again whenever a key changes. Values never get printed.
#   CF_ACCESS_AUD comes from the Cloudflare Access application (see DEPLOY.md step 2).
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.env
NAMES=(DATABASE_URL GROQ_API_KEY ANTHROPIC_API_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY CF_ACCESS_AUD)

value_of() {  # read KEY=value from .env without sourcing it (values may contain shell chars)
  grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\r\n'
}

for name in "${NAMES[@]}"; do
  v="$(value_of "$name")"
  if [ "$name" = GROQ_API_KEY ] && [ -z "$v" ]; then v="$(value_of GROQ_APIKEY)"; fi
  if [ -z "$v" ]; then echo "skip $name (not in .env)"; continue; fi
  if gcloud secrets describe "$name" --project "$GCP_PROJECT" >/dev/null 2>&1; then
    printf '%s' "$v" | gcloud secrets versions add "$name" --data-file=- --project "$GCP_PROJECT" >/dev/null
    echo "updated $name"
  else
    printf '%s' "$v" | gcloud secrets create "$name" --data-file=- --project "$GCP_PROJECT" >/dev/null
    echo "created $name"
  fi
done

# Let the Cloud Run runtime service account read them.
SA="$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
for name in "${NAMES[@]}"; do
  gcloud secrets add-iam-policy-binding "$name" --project "$GCP_PROJECT" \
    --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null 2>&1 || true
done
echo "done"
