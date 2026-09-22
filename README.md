# Better Email Filter

A local Gmail triage tool powered by [TypeSafe AI's Jev](https://docs.typesafe.ai/).
Classify messages as **Spam**, **Ignore**, or **Important**, with **Review** for
uncertain decisions. Correct labels in Gmail and reuse those corrections as
examples for future classifications.

This version supports **macOS**, one Gmail account per data directory, and a
Spanish/English classification policy. Credentials use macOS Keychain; Linux and
Windows credential storage is not implemented. It is an experimental personal
workflow: automated tests verify mechanics, not accuracy on your inbox.

**`process.sh` enables cleanup by default:** Spam moves to Trash, and Ignore and
Review are archived. Start with the label-only commands below or use
`./process.sh --no-cleanup` while assessing the classifications.

## Requirements

- macOS with Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- A Gmail account and a Google Cloud project with the Gmail API enabled.
- A Google OAuth **Desktop app** client for that project.
- A Jev API key. Classification and evaluation make requests to this paid external
  service; check the terms and pricing for your account.

## Install and configure

```sh
git clone https://github.com/miguelgarcia/better-email-filter.git
cd better-email-filter
uv sync --locked
mkdir -p private
cp config.example.toml private/profile.toml
```

Edit `private/profile.toml` before your first classification. For example, these
are fictional profile values:

```toml
body_words = 300
preferences = "" # Your own classification preferences or explicitly excluded services.

[profile]
full_name = "Alex Taylor"
name_variations = ["Alex Jordan Taylor", "AJ"]
company_names = []
```

Keep any threshold/model settings you want from the example file. Matching names
are clues rather than proof that an email is intended for you. There are **no
built-in country or bank exclusions**; put your own preferences in this local file.
Explicit preferences take precedence over general category defaults when applicable.

The default policy treats all promotions and clearly misdirected mail as spam,
routine paid receipts as ignore, and genuine reminders, unpaid bills, reply
requests, and consequential account notices as important. Adjust the preferences
and review examples to suit your needs.

### Connect Gmail

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a
   project and enable the **Gmail API** in that project.
2. Configure the OAuth consent screen/audience. If the app is in testing mode,
   add your Gmail account as a test user. Workspace policies may require approval
   from your administrator.
3. Create an OAuth client with application type **Desktop app**. Download its JSON
   into `private/client_secret.json`.
4. Authorize access:

```sh
uv run better-email auth gmail --client private/client_secret.json
```

Finish the browser flow, then check the terminal for `"saved": "gmail"`. The
browser callback alone does not confirm that Gmail API access works. A 403 with
`SERVICE_DISABLED` or `accessNotConfigured` usually means the Gmail API must be
enabled in the project owning the downloaded client. Missing permission errors
require authorizing again and granting access.

The app uses the `gmail.modify` OAuth scope for reading messages, applying labels,
archiving, and moving messages to Trash. It does not send emails or permanently
delete them. OAuth tokens are stored in macOS Keychain. See Google's
[Desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app)
and [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).

### Connect Jev

```sh
uv run better-email auth jev
uv run better-email probe
```

The first command prompts for the key without echoing it and saves it in Keychain.
`probe` sends a synthetic promotion to Jev; it does not access Gmail. You can also
supply `TYPESAFE_API_KEY` through your environment. Scheduled jobs do not inherit
interactive shell exports, so Keychain is the simpler choice for scheduling.
Never put keys or OAuth files in committed source files.

## First run: inspect before moving messages

```sh
uv run better-email --config private/profile.toml learn --activate
uv run better-email preview --limit 25
uv run better-email list --limit 25 --show-headers
uv run better-email apply --limit 25
```

- `learn --activate` snapshots preferences, profile, thresholds, and human examples.
- `preview` syncs Gmail, classifies eligible messages with Jev, and saves predictions.
  It makes no Gmail changes.
- `list` reads local state. Headers are omitted unless explicitly requested.
- Plain `apply` adds custom labels without archiving or trashing. It makes no Jev calls.

Labels are `BetterEmail/Spam`, `BetterEmail/Ignore`, `BetterEmail/Important`, and
`BetterEmail/Review`. The `proposed_label` is the actual planned label; a guessed
category such as `ignore` can still produce Review when confidence is low.

Each command defaults to a limit of 200. The shell wrapper defaults to 100:

```sh
./process.sh --no-cleanup       # Sync, learn, reclassify, apply labels only
./process.sh 250 --no-cleanup   # Same workflow with a larger per-step limit
./process.sh 100 --cleanup     # Also move eligible mail out of the inbox
./process.sh                   # 100 per step; cleanup is ON by default
```

The script loads `private/profile.toml` if it exists; otherwise it inherits the
stored policy. It stops on failure. Limits are 1–10000, apply separately to each
step, and do not represent a single total across the workflow. Reclassification
skips predictions already made with the active version and preserves human labels.

## Cleanup behavior

`uv run better-email apply --limit 100 --cleanup` adds these actions:

| Custom label | Action |
| --- | --- |
| Spam | Move the individual message to Gmail Trash |
| Ignore | Archive by removing the INBOX label |
| Review | Archive; retain the Review label for later inspection |
| Important | Keep in the inbox |

Read/unread state and category labels remain intact. Cleanup affects individual
messages, not entire conversations. The app never empties Trash or invokes
permanent deletion. Trashed mail remains subject to Gmail's own retention behavior.

Cleanup has its own write limit equal to `--limit`. It can move previously labeled
tracked messages as well as messages labeled in the current run. It rechecks
labels before each move, respects corrections, and skips stale/unapplied predictions,
conflicts, drafts, and messages already outside the inbox.

A local journal records each attempt. Pending operations are reconciled on the
next sync without blindly repeating writes. A message is attempted at most once:
if you restore it to the inbox, future cleanup leaves it there. Inspect `list` or
`status` for cleanup state; handle an `uncertain` move manually in Gmail. Avoid
concurrent category edits during writes because Gmail has no atomic read/check/write
operation. Archiving/trashing does not count as training feedback.

## Correct mistakes and learn

In Gmail, remove the incorrect BetterEmail category label and add the correct one.
Replace Review with Spam, Ignore, or Important when you resolve it. Reserve these
labels for your corrections and this app, not other filters or integrations.

```sh
uv run better-email sync
uv run better-email learn --activate
uv run better-email preview --reclassify --limit 100
uv run better-email apply --limit 100
```

The wrapper performs the same sequence. Feedback is collected even for tracked
messages that have left the inbox. Exact-message corrections take precedence over
predictions. Conflicting category labels suspend automation until resolved.
Removing all category labels also suspends the message.

You can explicitly confirm a correct prediction using the app's message ID from
`list` (not the RFC Message-ID header):

```sh
uv run better-email feedback confirm MESSAGE_ID important --language en
```

This records feedback locally; `apply` makes any needed Gmail label change. Reading,
archiving, or leaving a label unchanged is not confirmation. `feedback revoke
MESSAGE_ID` releases a correction/suspended message and invalidates old snapshots;
run `learn --activate` and `preview` afterward.

Learning reuses saved examples and preferences; it does **not fine-tune model
weights**. Up to six relevant training examples are selected using sender/subject
similarity. Approximately 20% of related example groups are held out for evaluation;
with little feedback, a partition may be empty. A correction is an example, not a
blanket rule for its sender. For a known reason that should generalize, edit your
private `preferences`, then run:

```sh
uv run better-email --config private/profile.toml learn --activate
```

With body context enabled, learning fetches missing excerpts for corrections before
snapshotting them; it makes no Jev calls. `learn --limit 200` bounds those fetches.
Repeat learning if `example_excerpts.remaining` is nonzero. Preview reports
`learning_refresh_needed` when feedback differs from its active snapshot.

## Privacy and local storage

**Jev receives sender, subject, up to 300 cleaned body words (also capped at 4,000
characters), selected example excerpts/categories, and your configured profile and
preferences.** Set `body_words = 0` and activate a new version to use headers only.
Previously cached excerpts are not erased by that setting.

Metadata sync does not read bodies. When an excerpt is needed, Gmail's full MIME
response is fetched transiently. The app prefers plain text or strips HTML locally,
omits attachment parts and common quoted replies/signatures, and saves only the
bounded excerpt. The full response may include embedded attachment bytes, which
are discarded. It never renders HTML, follows links, loads images, fetches attachment
references, or reads other thread messages for context. Text cleanup is heuristic.

OAuth tokens and Jev keys live in **macOS Keychain**. SQLite lives by default in
`~/Library/Application Support/BetterEmail` and stores headers, excerpts, labels,
predictions, feedback, policy versions, sync checkpoints, and action journals.
It is protected by filesystem permissions, not separate database encryption.
Use `--data-dir PATH` before the subcommand for another location; that path also
selects a separate credential namespace. Use it consistently.

```sh
uv run better-email feedback export private/feedback.json
uv run better-email feedback delete MESSAGE_ID
uv run better-email --config private/profile.toml feedback prune
```

Exports include saved excerpts. Pruning uses `retention_days` (zero retains feedback
until explicitly deleted). Deletion/pruning clears matching feedback and excerpts,
invalidates all learning snapshots, and suspends those messages against immediate
re-import. Operational headers, other messages' excerpts, exports, and external
backups remain. These commands make no Gmail changes.

`private/`, environment files, OAuth JSON patterns, databases, logs, raw mail,
virtual environments, and build outputs are ignored by Git. Keep all real personal
configuration and datasets in `private/` or outside the repository. Review staged
files before sharing; ignore patterns cannot identify every possible secret filename.

## Review thresholds and evaluation

Default review thresholds are category probability below 0.85 or confidence below
0.70; missing/truncated headers also require review. An optional extra evidence gate
is disabled by default. These scores are not measured accuracy guarantees. A body
excerpt being truncated alone does not force Review, but missing text never proves
that no action is required. Email text and examples are treated as untrusted data.

```sh
uv run better-email sync
uv run better-email learn
uv run better-email versions
uv run better-email evaluate --version CANDIDATE_ID --compare BASELINE_ID --output private/evaluation.json
uv run better-email activate CANDIDATE_ID
```

With an active version, `learn` without `--activate` creates a candidate. Evaluation
makes Jev calls on held-out examples and reports confusion matrices, precision/recall,
review rate, errors, important messages mislabeled low priority, token usage, and
optional language metrics. Thread/template groups used for training by either
compared version are excluded. This reduces obvious leakage, not every semantic duplicate.

Rollback uses `activate BASELINE_ID`. Rerun preview with `--reclassify` and apply to
update older predictions. Human labels survive rollback. The resolved model is
recorded, but `jev-latest` can change externally; a snapshot does not pin that alias.

## Backlog and synchronization

```sh
uv run better-email sync --backfill --query 'after:2026/01/01 before:2026/02/01' --limit 200
uv run better-email preview --limit 200
```

Queries control initial discovery/backfill, not a permanent classification filter.
Discovery is limited to the inbox. Incremental history detects later arrivals and
changes, including new replies in existing threads. When Gmail history expires,
all current inbox IDs and tracked messages are reconciled; remaining metadata work
stays queued. `status` shows queue size. Requests retry transient reads/inference
at most three times; uncertain Gmail writes are journaled rather than blindly retried.

## Run hourly with launchd

Once manual runs work, generate a personal plist from the project root. This uses
local paths and does not install or start anything:

```sh
uv run python - <<'PYTHON'
from pathlib import Path
import plistlib
import shutil

root = Path.cwd()
uv_path = shutil.which("uv")
if not uv_path:
    raise SystemExit("uv is not on PATH")
logs = root / "private" / "logs"
logs.mkdir(parents=True, exist_ok=True, mode=0o700)
logs.chmod(0o700)
config = {
    "Label": "local.better-email-filter",
    "ProgramArguments": ["/bin/bash", str(root / "process.sh"), "100"],
    "WorkingDirectory": str(root),
    "EnvironmentVariables": {
        "PATH": str(Path(uv_path).parent) + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    },
    "StartCalendarInterval": {"Minute": 0},
    "RunAtLoad": False,
    "StandardOutPath": str(logs / "process.log"),
    "StandardErrorPath": str(logs / "process-error.log"),
    "Umask": 63,
}
path = root / "private" / "local.better-email-filter.plist"
with path.open("wb") as handle:
    plistlib.dump(config, handle)
path.chmod(0o600)
print(path)
PYTHON
plutil -lint private/local.better-email-filter.plist
```

This runs the wrapper with **cleanup enabled**. Add `"--no-cleanup"` to its
`ProgramArguments` array for label-only runs, or change `"100"` for another limit.
Keep only one scheduled instance of this project; disable any older LaunchAgent
before installing another configuration.

Install and enable the job without `sudo`:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp private/local.better-email-filter.plist "$HOME/Library/LaunchAgents/"
launchctl enable "gui/$(id -u)/local.better-email-filter"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.better-email-filter.plist"
```

It runs at the top of each hour while you are logged in. Calendar runs missed while
the Mac sleeps are coalesced into one run after wake. Logs append over time:

```sh
launchctl print "gui/$(id -u)/local.better-email-filter"
tail -n 50 private/logs/process.log private/logs/process-error.log
```

A Keychain access prompt may require interaction on the first run. Disable the job
persistently with:

```sh
launchctl disable "gui/$(id -u)/local.better-email-filter"
launchctl bootout "gui/$(id -u)/local.better-email-filter"
```

After editing the private plist, stop the existing job and repeat the copy/enable/
bootstrap steps. Regenerate or update paths if you move the checkout or `uv`.
See Apple's [launchd guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

## Development

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
uv build
```

Tests use synthetic fixtures and mock Gmail/Jev transports. They do not require
credentials or access real mail. The package and command retain the name
`better-email`; the repository is named `better-email-filter`.

See [SPEC.md](SPEC.md) for behavior and [PLAN.md](PLAN.md) for implementation scope.
Attachment analysis, permanent deletion, model-weight fine-tuning, and non-macOS
credential storage are not implemented.
