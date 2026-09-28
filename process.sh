#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 [email-limit] [--cleanup | --no-cleanup]"
  echo "Run sync, learn --activate, preview --reclassify, and apply."
  echo "The limit defaults to 100 and must be between 1 and 10000."
  echo "--cleanup trashes Spam and archives Ignore/Review after labeling."
  echo "--no-cleanup keeps all messages in the inbox."
  echo "Cleanup is enabled by default in this script."
}

if [[ $# -eq 1 && ("$1" == "--help" || "$1" == "-h") ]]; then
  usage
  exit 0
fi

limit=100
limit_set=false
cleanup=true
cleanup_set=false
for argument in "$@"; do
  if [[ ("$argument" == "--cleanup" || "$argument" == "--no-cleanup") && "$cleanup_set" == false ]]; then
    cleanup_set=true
    if [[ "$argument" == "--cleanup" ]]; then
      cleanup=true
    else
      cleanup=false
    fi
  elif [[ "$argument" =~ ^[1-9][0-9]{0,4}$ && "$limit_set" == false ]]; then
    limit="$argument"
    limit_set=true
  else
    usage >&2
    exit 2
  fi
done
if ((limit > 10000)); then
  usage >&2
  exit 2
fi

# Resolve relative configuration paths from the project, regardless of launch directory.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is required and must be on PATH." >&2
  exit 1
fi

# The Python runner holds a workflow lock while each CLI command uses its own state lock.
# Environment settings for data directory and credential backend reach every step.
if [[ "$cleanup" == true ]]; then
  exec uv run --locked python -m better_email.workflow --limit "$limit"
else
  exec uv run --locked python -m better_email.workflow --limit "$limit" --no-cleanup
fi
