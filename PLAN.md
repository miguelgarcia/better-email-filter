# Implementation status

## Implemented

- Python CLI with reproducible uv dependencies and macOS Keychain credentials.
- Gmail metadata/history synchronization, durable queues, expired-history recovery.
- Local bounded MIME text extraction; explicit Jev payload allowlists.
- Preview, cached decisions, custom-label application, and review routing.
- Gmail correction capture, conflict suspension, atomic label-write acknowledgement.
- Versioned policy/profile/examples, candidate evaluation and rollback.
- Feedback confirmation, revocation, export, deletion, and explicit retention pruning.
- Opt-in CLI cleanup with Trash/archive operations, action journaling, and restore protection.
- Shell workflow with a configurable per-step limit and cleanup enabled by default.
- Generic setup, privacy, learning, and hourly launchd instructions in README.md.

## Validation

Synthetic tests cover classification plumbing, privacy boundaries, body parsing,
feedback, migrations, interrupted writes, cleanup, shell arguments, and evaluation.
They make no live Gmail/Jev requests and do not measure inbox classification accuracy.
Ruff and package builds are part of the documented development checks.

## Remaining work

- Measure quality using representative human-reviewed messages and compare policy variants.
- Tune thresholds/retrieval based on measured important misses, spam precision, and review load.
- Additional operating systems need an explicit credential-storage implementation.
- Attachment analysis and model-weight fine-tuning require separately designed workflows.
