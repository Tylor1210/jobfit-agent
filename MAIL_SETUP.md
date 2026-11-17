# Mail setup: application tracking

The tracker reads four inboxes **read-only** and only fetches application-related mail
(the sender and subject filters in `config/mail.yaml`). It stores extracted fields and a snippet
of 200 characters or less, never full emails. Nothing is forwarded, moved, deleted or marked
read.

Every value below goes in `.env` (gitignored) and, for the daily run, in GitHub repo secrets.
**Never** put addresses, tokens or passwords in `config/mail.yaml`: this repo is public.

| Label in config/mail.yaml | Account | Connector | Secrets |
|---|---|---|---|
| `gmail_main` | first Gmail address | Gmail API, `gmail.readonly` | `GMAIL_MAIN_REFRESH_TOKEN` |
| `gmail_alt` | second Gmail address | Gmail API, `gmail.readonly` | `GMAIL_ALT_REFRESH_TOKEN` |
| `workspace` | custom-domain mailbox (Google Workspace) | Gmail API, `gmail.readonly` | `WORKSPACE_REFRESH_TOKEN` |
| `yahoo` | Yahoo address | IMAP, app password | `YAHOO_IMAP_USER`, `YAHOO_APP_PASSWORD` |

Accounts you skip are reported as "not set up" and never fail a run.

## 1. Google OAuth client (once, about 10 minutes)

All three Google accounts share one OAuth client. Use the same Google Cloud project as the
Cloud Run deploy (or any project).

1. [console.cloud.google.com](https://console.cloud.google.com) → **APIs & Services → Library**
   → enable **Gmail API**.
2. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding/Audience):
   - User type: **External**. App name: `jobcheck`. Support and developer email: yours.
   - **Data access → Add scopes** → `https://www.googleapis.com/auth/gmail.readonly`.
   - **Audience → Publishing status → Publish app** (switch to **In production**). Don't submit
     for verification.
3. **Clients → Create client** → Application type **Desktop app** → name `jobfit-cli`.
4. Copy the client ID and secret into `.env`:
   ```
   GOOGLE_OAUTH_CLIENT_ID=...
   GOOGLE_OAUTH_CLIENT_SECRET=...
   ```

### Why "In production", and what that means

- In **Testing** status, Google expires refresh tokens after **7 days**, so the daily sync would
  stop every week until you re-authorize.
- In **In production** status without verification, tokens don't expire on a schedule. You'll see
  a "Google hasn't verified this app" screen once per account. Click **Advanced → Go to jobcheck
  (unsafe)**: it's your own app, reading your own mail. Unverified apps are capped at 100 users,
  which doesn't matter here.
- Other options, if you'd rather: stay in Testing and run `jobfit mail auth <label>` weekly, or
  (Workspace account only) put the OAuth client in a project owned by your Workspace org and set
  the user type to **Internal**, which never needs verification or expires.
- A token still dies if you revoke access, change the account password (for Gmail scopes), or
  don't use it for 6 months. The sync then skips that account, records "Google refused the
  refresh token... Run `uv run jobfit mail auth <label>`" on the Applications page and in the
  run log, and keeps syncing the others.

## 2. Authorize each Google account (2 minutes each)

From the project folder:

```bash
uv run jobfit mail auth gmail_main
```

A browser opens. **Pick the account that matches the label**, accept the warning (step 1), and
allow read-only Gmail access. The token is written to `.env` (it is not printed). Repeat:

```bash
uv run jobfit mail auth gmail_alt
```

```bash
uv run jobfit mail auth workspace
```

**Workspace only:** if sign-in says the app is blocked, open the Google Admin console →
**Security → Access and data control → API controls → Manage third-party app access** → add the
client ID from step 1 and set it to **Trusted**.

## 3. Yahoo (5 minutes)

1. Yahoo needs 2-step verification on. Go to **Account Info → Account security**.
2. **Generate app password** (sometimes under "Other ways to sign in") → name it `jobfit` →
   copy the 16-character password.
3. Add to `.env`:
   ```
   YAHOO_IMAP_USER=your-yahoo-address
   YAHOO_APP_PASSWORD=the-app-password
   ```

The connector opens the inbox read-only (IMAP `EXAMINE`) and fetches with `BODY.PEEK[]`, so
messages stay unread.

## 4. Backfill locally first

```bash
uv run jobfit mail backfill --days 90
```

It prints counts per account, then the summary: applications found, matched to postings, new,
flagged for review, and counts by status. Open the dashboard's **Applications** page and work
through **Needs review**. Rerunning is safe: emails are deduplicated by Message-ID. If the daily
cost cap stops it, run it again tomorrow and it continues.

## 5. Turn on the daily sync

Add each value from `.env` as a GitHub secret (repo → Settings → Secrets and variables →
Actions), or from the project folder:

```bash
for k in GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET GMAIL_MAIN_REFRESH_TOKEN GMAIL_ALT_REFRESH_TOKEN WORKSPACE_REFRESH_TOKEN YAHOO_IMAP_USER YAHOO_APP_PASSWORD; do v=$(grep "^$k=" .env | cut -d= -f2-); [ -n "$v" ] && gh secret set "$k" --body "$v"; done
```

The `Mail sync` step in `.github/workflows/daily-ingest.yml` runs after the job ingest. It never
fails the workflow, and its logs show only counts per account label, because Actions logs on a
public repo are public.

## Checklist

- [ ] Gmail API enabled; consent screen External, `gmail.readonly`, **In production**
- [ ] Desktop OAuth client ID/secret in `.env`
- [ ] `jobfit mail auth gmail_main` / `gmail_alt` / `workspace` (each token lands in `.env`)
- [ ] Workspace: client ID trusted in Admin console (only if sign-in was blocked)
- [ ] Yahoo app password in `.env`
- [ ] `jobfit mail backfill --days 90` run and review queue cleared
- [ ] All seven values added as GitHub secrets
