# Implementation status

## Implemented

- Python CLI with reproducible uv dependencies, macOS Keychain, and explicit Linux file credentials.
- Gmail metadata/history synchronization, durable queues, expired-history recovery.
- Local bounded MIME text extraction; explicit Jev payload allowlists.
- Preview, cached decisions, custom-label application, and review routing.
- Gmail correction capture, conflict suspension, atomic label-write acknowledgement.
- Versioned policy/profile/examples, candidate evaluation and rollback.
- Feedback confirmation, revocation, export, deletion, and explicit retention pruning.
- Opt-in CLI cleanup with Trash/archive operations, action journaling, and restore protection.
- Shell workflow with a configurable per-step limit and cleanup enabled by default.
- Atomic credential updates, verified export/import, private consistent SQLite backups.
- A lock across the full workflow, shared by manual and scheduled runs using the same data directory.
- Generic setup, privacy, learning, Mac launchd, and Linux migration/cron instructions in README.md.

## Validation

Synthetic tests cover classification plumbing, privacy boundaries, body parsing,
feedback, migrations, interrupted writes, cleanup, shell arguments, and evaluation.
They make no live Gmail/Jev requests and do not measure inbox classification accuracy.
Ruff and package builds are part of the documented development checks. CI runs the
synthetic tests on Linux and macOS, including credential permissions, migration,
atomic refresh failures, account mismatches, and overlapping workflows.

## Remaining work

- Measure quality using representative human-reviewed messages and compare policy variants.
- Tune thresholds/retrieval based on measured important misses, spam precision, and review load.
- Windows needs a separate credential-storage and process-lock implementation.
- Attachment analysis and model-weight fine-tuning require separately designed workflows.
