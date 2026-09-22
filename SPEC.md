# Better Email Filter: behavior specification

## Scope

A macOS command-line application for one Gmail account per local data directory.
It uses Jev for three-category email classification, supports a Spanish/English
policy, and adapts through explicit human feedback. Configuration and personal
identity details belong in ignored private files, not source control.

## Classification

- Spam: unwanted promotions, solicitation, scams, or clearly misdirected mail.
- Ignore: legitimate routine information requiring no attention, such as paid receipts.
- Important: mail to read or act on, including reminders, unpaid bills, and reply requests.
- Review: a workflow label for uncertain predictions, not a fourth model category.

Explicit user preferences can override these defaults. No user-specific geographic,
provider, or identity assumptions are built into the shared application. Matching
names alone do not prove relevance; sender identity alone does not determine category.

Provider requests use an explicit allowlist: sender, subject, configured bounded
body excerpt and availability/truncation flags, selected examples with human categories,
and policy/profile context. Defaults are 300 words, 4,000 excerpt characters, and six
relevant examples. Set body_words to zero for headers only. Raw Gmail responses,
attachments, other headers, local IDs, and language tags are excluded from requests.

Full MIME responses are transiently fetched only when excerpts are needed. Prefer
plain text, otherwise strip HTML without rendering or visiting links. Skip attachments
and forwarded MIME messages; remove common reply quotes/signatures heuristically.
Email content and examples are untrusted data.

## Workflow and cleanup

Sync reconciles Gmail history and human label edits before classification or writes.
Preview makes inference calls and saves predictions without changing Gmail. Apply
uses current-version predictions or explicit overrides and changes custom labels.

Plain apply retains inbox membership. With --cleanup, Spam moves to Trash and
Ignore/Review are archived; Important remains in the inbox. The shell wrapper defaults
to cleanup enabled and provides --no-cleanup. Read/unread state is preserved. No
permanent deletion, sending, unsubscribing, or Gmail Spam-folder moves are implemented.

Cleanup rechecks labels, skips conflicts and stale/unapplied decisions, and journals
at most one attempt per message. Pending outcomes are reconciled; uncertain writes
are not blindly retried. Manual inbox restores are respected. Operations are message-
level, not thread-level. Gmail cannot atomically compare labels and perform writes.

## Feedback and evaluation

Only explicit confirmations or category changes unexplained by application writes
are training feedback. Multiple category labels suspend automation; removing labels
also suspends it. Reading, archiving, deleting, and unchanged labels are not confirmation.
Exact-message human overrides survive learning, reclassification, and rollback.

Learning snapshots questions, profile, preferences, thresholds, and human examples;
it does not change model weights. Relevant examples are selected by sender/subject.
Related threads and normalized templates stay grouped, with roughly 20% held out.
Evaluation excludes groups used for training by any compared version and reports
category metrics, review/errors, important misses, and optional language breakdowns.
Small or biased datasets do not establish accuracy. Numeric quality targets must be
chosen and measured by each user.

## Storage and reliability

OAuth and Jev credentials use macOS Keychain. Private-permission SQLite stores message
references, headers, capped excerpts, feedback, version snapshots, predictions, sync
progress, and action journals. Full messages and attachments are not retained. SQLite
is not separately encrypted. Schema updates preserve existing state.

Feedback export and deletion are provider-independent. Deleting feedback removes
associated excerpts and invalidates learning snapshots, while operational headers,
other cached messages, external exports, and backups remain. Retention defaults to
explicit deletion; configured expiry is enforced by the prune command.

Incremental Gmail history uses a durable queue. Expired history triggers inbox-ID
rediscovery and tracked-message reconciliation. Limits bound discovery/inference/
write batches separately. Transient reads and inference have bounded retries; writes
are journaled. Commands lock their data directory to avoid concurrent state mutation.

## Scheduling and validation

The README documents a per-user hourly macOS LaunchAgent generated with local paths.
Installation is explicit and requires working manual authentication first.

Automated tests use synthetic mail and mocked transports. Acceptance checks cover
payload boundaries, MIME extraction, Gmail corrections, restart recovery, cleanup,
manual restores, migration, evaluation separation, and CLI/shell argument handling.
Real-mail classification quality requires separate user-reviewed evaluation.
