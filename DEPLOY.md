# Deploy: jobcheck.tycodes.app

Cloud Run runs the Docker image; a Cloudflare Worker serves it at `jobcheck.tycodes.app`;
Cloudflare Access puts a login (one-time code to your email) in front. The app also checks the
Access token itself, so the raw `*.run.app` URL returns 403 to anyone without a valid login.

```
browser -> Cloudflare Access (login) -> Worker (jobcheck.tycodes.app) -> Cloud Run -> Supabase
```

Expected cost at personal use: $0 on Cloud Run's free tier (scale to zero, one instance) and $0
for Access (free up to 50 users). The LLM calls are the only spend: Groq first, then Claude.

## 1. Google Cloud (one time, ~10 min)

```bash
brew install --cask google-cloud-sdk
gcloud auth login
gcloud projects create jobcheck-tycodes      # or reuse a project; update deploy/config.env
gcloud config set project jobcheck-tycodes
```

Link a billing account in the console (Billing -> link). Cloud Run's free tier still requires
one. Set a budget alert while you're there (Billing -> Budgets, e.g. $5).

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
```

## 2. Cloudflare Access (one time, ~5 min)

1. Cloudflare dashboard -> **Zero Trust**. First time: pick a team name (e.g. `tycodes`), Free plan.
   Put `<team>.cloudflareaccess.com` in `deploy/config.env` as `CF_ACCESS_TEAM_DOMAIN`.
2. **Access -> Applications -> Add -> Self-hosted**. Domain: `jobcheck.tycodes.app`.
   Session duration: 1 week (or your preference).
3. Policy: **Allow**, Include -> Emails -> your email. Login method: One-time PIN.
4. Save, then open the app's **Overview** and copy the **Application Audience (AUD) Tag**.
   Add it to `.env` as `CF_ACCESS_AUD=...` (it goes to Secret Manager in the next step).

## 3. Deploy the app

```bash
bash deploy/secrets.sh      # .env -> Secret Manager (values are never printed)
bash deploy/cloudrun.sh     # build with Cloud Build + deploy; prints the Cloud Run URL
```

The first build takes ~10 minutes (Chromium + the embedding model are baked into the image).
The script prints two curl checks: `/api/jobs` must be **403**, `/api/health` **200**.

## 4. Point the domain at it

```bash
cd deploy/worker
# set ORIGIN in wrangler.toml to the Cloud Run URL from step 3
npx wrangler login
npx wrangler deploy         # also creates the jobcheck.tycodes.app DNS record
```

Open https://jobcheck.tycodes.app -> enter your email -> paste the PIN -> dashboard.

## Updating

Code changes: `bash deploy/cloudrun.sh` again. New keys: update `.env`, then `bash deploy/secrets.sh`
and redeploy (Cloud Run reads secrets at startup).

## Notes

- **Cold start:** the first visit after ~15 idle minutes takes 10 to 20 s while the container
  starts (it also applies migrations and checks the profile index).
- **Background work:** tailoring runs after the request returns, so the service uses
  `--no-cpu-throttling`. The instance bills while it's awake, then scales to zero.
- **PDFs** are stored in Postgres (`tailor_pdfs`), not on the container disk, so downloads
  survive restarts.
- **Voice samples** (`profile/voice/`) are not in the image (they're your private letters).
  Cover letters drafted on Cloud Run use the written style rules only.
- **Daily ingestion** keeps running on GitHub Actions; it doesn't depend on this deploy.
