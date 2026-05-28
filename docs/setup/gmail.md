# Gmail backend setup (PR #27)

This page is the runbook for using the real Gmail backend with
`wolf` (`--gmail-backend gmail`). The default `--gmail-backend fake`
needs no setup at all and is the only backend the test suite uses.

## What `wolf` does NOT do

- It does **not** implement Gmail / SMTP / IMAP send. There is no
  `users.messages.send` call anywhere in this codebase.
- It does **not** implement an OAuth browser login flow. There is
  no browser redirect handler, no consent screen, no installed-app
  flow.
- It does **not** refresh the access token. If the token expires,
  the next call will fail with `gmail:*: HTTP 401` and you must
  refresh the file out of band.
- It does **not** open arbitrary file paths to find a token. You
  must pass `--credentials-path` explicitly.

If any of those limitations are blockers for your workflow, stop
here. Don't work around them by hand-rolling extras inside this
repo — that is exactly what the no-send policy is meant to
prevent.

## Required scopes

The access token must be issued with at least:

- `https://www.googleapis.com/auth/gmail.readonly`
  — needed by `gmail-search`, `gmail-read`, `gmail-summarize`,
  `gmail-thread`, `gmail-search-summarize`.
- `https://www.googleapis.com/auth/gmail.compose`
  — needed by `gmail-draft`. (`compose` is intentionally narrower
  than `gmail.send` / `gmail.modify`.)

If your token only carries `gmail.readonly`, `gmail-draft` will
fail at HTTP 403; the failure is logged in the audit jsonl with
`outcome=draft_failed` and `error_label`.

## Obtaining an access token

Pick one. Whichever path you use, the deliverable is a JSON file
on local disk shaped like:

```json
{"access_token": "ya29.A0..."}
```

A `refresh_token`, `expiry`, or `scopes` field may be present;
`wolf` ignores them.

### Option 1 — Google OAuth 2.0 Playground (manual)

1. Visit https://developers.google.com/oauthplayground/.
2. In the left pane, scroll to **Gmail API v1** and select the
   scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
3. Click **Authorize APIs** and complete the consent screen for
   the Google account that owns the mailbox you want to read.
4. Click **Exchange authorization code for tokens**.
5. Copy the `access_token` value from the response panel.
6. Save it to a JSON file outside the repo (see "Where to store
   the file" below).

### Option 2 — `gcloud auth print-access-token` (Workspace contexts)

If your Google account has Workspace / Cloud access and you have
`gcloud` configured locally:

```sh
gcloud auth login --update-adc
TOKEN=$(gcloud auth print-access-token \
  --scopes=https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.compose)
printf '{"access_token": "%s"}\n' "$TOKEN" > ~/secrets/gmail_token.json
chmod 600 ~/secrets/gmail_token.json
```

Note: Workspace-style tokens often have shorter lifetimes (an
hour) than OAuth-Playground tokens. Plan to re-run the snippet
above whenever a call returns 401.

### Option 3 — your own OAuth client

If you already have a registered OAuth client (Desktop or Web)
and a refresh-token flow elsewhere, just dump the latest
`access_token` to the JSON shape above. `wolf` does not need the
client id, client secret, or refresh token.

## Where to store the file

- **NOT inside the project root.** Don't put it under
  `./secrets/`, `./credentials/`, `./tokens/`, `./private/`, or
  `./.env*` — those are denied by `SensitivePathGuard` and are
  meant for local secrets that wolf must never read.
- Recommended: `~/secrets/gmail_token.json` with mode `0600`.
- **Never commit it.** Add the file's absolute path to your
  global `.gitignore` if you must keep it near the repo.

The `--credentials-path` flag accepts both absolute paths and
paths relative to your current working directory; relative paths
are resolved against the cwd, not against `--project-root`.

## Running the real backend

```sh
PYTHONPATH=src python3 -m wolf.cli gmail-search \
    --gmail-backend gmail \
    --credentials-path ~/secrets/gmail_token.json \
    --query "from:alice meeting" --limit 5

PYTHONPATH=src python3 -m wolf.cli gmail-search-summarize \
    --gmail-backend gmail \
    --credentials-path ~/secrets/gmail_token.json \
    --query "from:alice meeting" \
    --llm-backend fake --output text
```

## Docker

The default `docker compose run --rm wolf` container runs with
`network_mode: none`. The real Gmail backend cannot make outbound
calls there and will fail at `stage=gmail_search` /
`stage=gmail_read` / `stage=gmail_create_draft` with
`reason=gmail:*: network error`.

If you want to exercise the real backend inside Docker, write a
compose override that opens outbound https only (e.g. via a
proxy) and mounts the credentials path read-only. That setup is
out of scope for this PR.

## Token rotation and the audit log

- Tokens expire. The expected failure mode is
  `gmail:*: HTTP 401`. Re-issue the token and retry.
- **PR #28 broadens audit coverage.** Every `gmail-*` command
  writes one event to `var/audit/audit.jsonl`:
  `gmail.search`, `gmail.read`, `gmail.thread`,
  `gmail.search_summarize`, `gmail.create_draft`. Each event
  carries only metadata (provider, counts, ids, lengths). The
  access token, raw mail body, draft body, and the search
  query string itself are NEVER recorded.
- `AuditLogger._mask()` additionally redacts any key matching
  `token` / `credential` / `secret` / `auth` etc., so an
  accidental field would be redacted on write.
- If the audit write fails (disk full, permission), the CLI
  returns `stage=audit_log` with exit 2 and refuses to claim
  success. For `gmail-draft` this can happen *after* a draft
  has already been created on Gmail's side; reconcile by hand.

## What still works without the real backend

Every `gmail-*` subcommand defaults to `--gmail-backend fake`. The
fake backend ships three synthetic messages in two threads and is
sufficient for:

- Trying out the CLI shape.
- Running the test suite (including in `network_mode: none`).
- Verifying that audit events are emitted with no body / token
  leakage.

You do not need a real token to use any of the safety features.
