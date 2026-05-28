# v0.1 known limitations

This page lists the constraints a v0.1 user should expect to hit.
Each one is intentional and either has a planned follow-up or is
documented as out of scope.

## Search

- **Substring is exact.** `search-files` (without `--semantic`) is a
  case-insensitive substring match. No stemming, no fuzzy, no regex.
  A query for "summarising" will not match "summarize".
- **Semantic requires a built embedding index.** `index-files
  --embed` has to run before `search-files --semantic`. There is no
  auto-refresh and no detection that the project has changed since
  the last build.
- **Embedding-model mismatch is not detected.** If the index was
  built with `nomic-embed-text` but you query with a different
  model, the CLI does not error; the resulting scores will just be
  meaningless. The model name lives in the index JSON
  (`embedding_model`); a runtime check is planned.
- **Search is pure-Python cosine.** For repositories with a few
  hundred files this is fine. Past several thousand entries the
  query-time cosine loop becomes the bottleneck. The plan is to
  swap in `array` / `numpy` without changing the public API when
  this matters.
- **No metadata filters in search.** You cannot say "only `*.md`
  files". Filter at index time with `--include` / `--exclude`
  instead.
- **No hybrid retrieval.** Substring and semantic results are not
  fused. Pick one mode per command.

## Embeddings

- **No chunked embeddings.** Each file contributes a single vector
  derived from the first `--embedding-input-bytes` (default 4 KiB).
  Long files have their tails ignored.
- **Single-vector-per-file.** No section-level or paragraph-level
  embedding. A planned follow-up will add chunked vectors and
  aggregate score via max-of-chunks.
- **The fake embedding adapter is for tests.** It produces stable
  vectors from character frequency but is not semantically
  meaningful. Use the Ollama adapter (`nomic-embed-text` or
  similar) for real retrieval.

## Summarization

- **Chunked summarize is sequential.** A 32-chunk file means 33
  LLM calls (32 chunks + 1 aggregate). Ollama on a Mac can take
  tens of seconds for this. No parallelism, no streaming.
- **Aggregate quality depends on the LLM.** With `nomic-embed-text`
  + `llama3.1` on consumer hardware the aggregate is usable but not
  brilliant for long inputs. Tune `--chunk-size`, `--max-chunks`,
  and `--limit` for your model and machine.
- **`--no-chunk` may exceed model context.** Passing a multi-MB
  file with `--no-chunk` will likely fail at the provider stage
  with an Ollama error.

## Source formats

- **Text only.** Markdown, plain text, RST, and Python source by
  default. `--include` lets you broaden the file extensions but
  the binary-detection guard (NUL bytes / high non-text ratio)
  still rejects non-text content.
- **No PDF, OCR, image, audio, or video.** No plans to add these
  in v0.1.x; tracking for v0.2.

## Integrations

- **Gmail: read + draft only. Never send.** PR #26 adds opt-in
  Gmail integration via `gmail-search`, `gmail-read`,
  `gmail-summarize`, and `gmail-draft`. PR #27 adds the
  workflow-level commands `gmail-thread` and
  `gmail-search-summarize` plus AuditLogger integration for
  `gmail-draft`. The CLI calls the Gmail REST API over `urllib`
  (stdlib only); no `google-api-python-client` is bundled. There
  is **no** `users.messages.send`, no SMTP, no IMAP.
  `gmail-draft` creates a Gmail draft via `users.drafts.create`;
  you must finish or discard it inside Gmail yourself. The
  default backend is the in-memory `FakeGmailClient`; the real
  backend requires an explicit `--credentials-path` to a JSON
  file with `{"access_token": "..."}`. No OAuth browser login;
  no refresh-token flow. The token is never refreshed by this
  CLI. See [`docs/setup/gmail.md`](setup/gmail.md) for how to
  obtain a token by hand.
- **Gmail draft audit fails closed.** Every `gmail-draft` call
  writes one `action_kind=gmail.create_draft` event to
  `var/audit/audit.jsonl`. If the audit write itself fails
  (disk full, permission), the CLI returns `stage=audit_log`
  with exit 2 even though the draft has already been created
  on Gmail's side. Reconcile the audit log and the Gmail Drafts
  folder by hand in that case.
- **Local mail (`.eml` / `.mbox` / Maildir) does not send either.**
  v0.2 added local `.eml` and `.mbox` read / search / draft via
  `mail-summarize`, `mail-search`, `mail-draft` (PR #22), and
  v0.3 added local threading + combined search-summarize via
  `mail-thread` and `mail-search-summarize` (PR #25). The
  original `summarize-email --text "..."` command also remains.
  None of these connect to any mailbox, send any mail, or save
  anything to disk. The draft command returns the proposed reply
  body in JSON; the user is responsible for actually sending it.
- **Datetime filtering is UTC-only and date-granular.** All five
  mail subcommands accept `--filter-since YYYY-MM-DD` and
  `--filter-until YYYY-MM-DD` (PR #25). `since` is the start and
  `until` is the end of the named UTC day (inclusive). Message
  `Date:` headers without timezone info are treated as UTC. Mail
  whose Date header is missing or unparseable is skipped when
  either flag is set. There is no time-of-day, no per-timezone
  filter, and no "last 7 days" relative shorthand.
- **Mail threading is heuristic, not authoritative.** `mail-thread`
  (and `mail-search-summarize --threaded`) cluster messages using
  `Message-ID` / `In-Reply-To` / `References` plus a
  normalized-subject fallback (`Re:` / `Fwd:` / `FW:` / `AW:`
  stripping). Forwarded mails that drop the lineage headers may
  end up in the wrong cluster, and unrelated mail that happens to
  share a subject after normalization will collide.
- **No `.msg` (Outlook), PGP / S/MIME, or attachment content
  extraction.** Mail attachments carry filename / content_type /
  size_bytes metadata only; their bytes are never read into the
  body. PDF / docx / image content inside attachments is still
  not parsed.
- **No calendar.** Task / calendar extraction is described in
  `CLAUDE.md` as a planned capability but not in v0.1 / v0.2.
- **No real robot motion.** `robot-preflight` is dry-run only;
  there is no `execute_motion` call in the CLI code path. The
  Router has the structure (preflight stage, audit log) but no
  live actuator interface.

## Operational

- **No GUI / TUI / web UI.** CLI only.
- **No background daemon.** Nothing watches the filesystem.
- **No incremental refresh.** Re-run `index-files` (and again with
  `--embed` if you use semantic) whenever the corpus changes.
- **The audit log grows unbounded.** `var/audit/audit.jsonl` is
  append-only. There is no rotation. Rotate it yourself if it
  matters.
- **`network_mode: none` is the default Docker posture.** The
  Ollama backend will not work from inside the default container.
  A future compose override can opt into a network-allowed
  profile.

## Safety guards

- **Prompt-injection scanning is keyword-based.** The list of
  forbidden markers lives in
  `src/wolf/safety/prompt_injection.py`. It catches the literal
  strings; paraphrased injections will get through. An LLM-based
  classifier is a planned hardening step.
- **TOCTOU race between checks and reads.** The
  `ProjectBoundaryGuard` and `SensitivePathGuard` run before
  `read_text_file` opens the file. A racing symlink swap between
  the two is not defended against. The mitigation (using
  `os.open(O_NOFOLLOW)` on the read path) is a planned follow-up.
- **Audit log doesn't include cryptographic chaining.** If a
  reviewer needs tamper-evident history, the per-line hash chain
  is a planned addition.

## Where to file follow-ups

For now: open a PR with the change. The repository follows the
PR workflow documented in `docs/dev/pr_workflow.md`. The
attribution guard is enforced at commit / push time and via the
GitHub Actions workflow `.github/workflows/no-attribution.yml`.
