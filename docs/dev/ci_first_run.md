# CI first-run verification runbook

This is the one-time procedure for confirming that the No attribution
guard workflow added in PR #11 (and hardened in PR #12) actually runs on
GitHub Actions after it lands on `main`. The local checks in PR #11 are
static; only an actual workflow execution proves the CI gate works.

## When to do this

The first opportunity is **the next PR opened against `main` after PR #11
has been merged**. Any PR works — including a documentation-only change.
You can also do this on PR #11 itself before merge: GitHub runs the
workflow on the PR branch because the workflow file exists in that branch.

## Which check to look for

In the PR's "Checks" tab the workflow appears as:

- **Workflow name**: `No attribution guard`
- **Job name**: `guard`

The check is also visible in the "Actions" tab at the repository level,
filtered by workflow name `No attribution guard`.

## Verifying a successful run

A green check (`✓`) on the `guard` job is the success signal. Expand the
job to confirm each step ran:

1. `Checkout repository` — green
2. `Show repo context` — green; the log prints `event_name=`,
   `ref=`, `sha=`, and a one-line `git log` summary
3. `Make guard executable` — green
4. `Scan HEAD commit (message + identity)` — green; the log is empty on
   success (the guard prints nothing when clean)
5. `Scan current git identity` — green; same convention
6. `Extract PR body to a temp file (pull_request only)` — green on a
   `pull_request` event; this step is **skipped** on a `push` event.
   The log shows `wrote PR body (<N> chars) to /tmp/wolf-ci-pr-body.txt`
   for non-empty bodies, or `PR body is empty; writing empty file for
   guard` for empty ones
7. `Scan PR body (pull_request only)` — green; same skip behavior on
   `push`

Record the workflow run URL (e.g.,
`https://github.com/yuta4869/wolf1/actions/runs/<run-id>`) so a future
auditor can replay the verification. The URL can be added as a comment on
the verifying PR or kept in a private note; it does not need to live in
this docs file.

## Verifying the PR body scan specifically

To prove the PR body extraction worked, look for either log line in step
6:

- `wrote PR body (<N> chars) to /tmp/wolf-ci-pr-body.txt` — non-empty body
  was scanned
- `PR body is empty; writing empty file for guard` — empty body, scan
  still happened

If neither line appears, the conditional `if: github.event_name ==
'pull_request'` did not match. That can happen if you are looking at a
`push` event (no PR context); open a real PR to trigger the body scan.

## Verifying the `edited` re-scan (PR #12 hardening)

After the initial PR opens:

1. Note the workflow run number from the `opened` event.
2. Click "Edit" on the PR title or body and save a trivial change.
3. Watch for a new workflow run with `event_name=pull_request` and
   `action: edited` (visible in the `Show repo context` step or the
   Actions tab metadata).
4. Confirm the new run is green and that step 6 again prints
   `wrote PR body (<N> chars) ...` with the updated character count.

A run that does NOT appear after a body edit means the workflow's
`on.pull_request.types` does not include `edited`. Check the workflow
file on the default branch.

## Failure investigation

A red check (`✗`) means the guard found a forbidden marker. The failing
step's log identifies which marker and which input. Read top to bottom:

- If `Scan HEAD commit` fails, the commit message or git identity at
  HEAD contains a forbidden marker. Look for `FORBIDDEN attribution
  detected:` and the `commit:HEAD-message` / `commit:HEAD-author` /
  `commit:HEAD-committer` label.
- If `Scan current git identity` fails alone, the `git config`
  `user.name` / `user.email` on the runner is unexpected. This is rare;
  the runner inherits identity from the committed history, so this
  usually surfaces a malformed author line.
- If `Scan PR body` fails, the PR body contains a forbidden marker. The
  log identifies the line. Edit the PR body to remove the marker; the
  `edited` re-scan trigger will run automatically.
- If `Extract PR body to a temp file` fails (rare), the GitHub event
  payload was malformed. Re-run the workflow from the Actions tab. If
  the failure persists, file an issue describing the event payload.

The workflow run page also exposes the raw event payload under
`Set up job` → `event.json`, useful for debugging unexpected step skips.

## Recording the verification

After the first green run, add a short note to the PR description or to
this file's footer:

```
First CI run verified: <date> / <PR number> / <run URL>
```

This converts an abstract gate into a worked example for new
contributors.

## What this runbook does NOT cover

- The unit test suite is not run by this workflow. That gap is the
  follow-up `wolf-tests.yml` mentioned in PR #11's next-steps.
- The workflow does not currently fail for paraphrased attribution
  (substring match only). PR #11 / #12 acknowledge this limitation.
- This runbook is the manual verification path. It is not itself
  enforced; once the first verification has happened, future PRs can
  trust the gate exists.
