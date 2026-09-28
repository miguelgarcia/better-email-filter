"""Run the complete scheduled workflow under one cross-process lock."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .cli import positive
from .domain import AppError
from .local import default_data_dir, locked_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=positive, default=100)
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args(argv)
    data_dir = default_data_dir()
    base = [sys.executable, "-m", "better_email.cli", "--data-dir", str(data_dir)]
    config = ["--config", "private/profile.toml"] if Path("private/profile.toml").is_file() else []
    limit = ["--limit", str(args.limit)]
    commands = [
        ["sync", *limit],
        [*config, "learn", "--activate", *limit],
        ["preview", "--reclassify", *limit],
        ["apply", *limit, *([] if args.no_cleanup else ["--cleanup"])],
    ]
    try:
        with locked_directory(data_dir, "workflow.lock"):
            for command in commands:
                result = subprocess.run(base + command)
                if result.returncode:
                    return result.returncode
    except AppError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, KeyboardInterrupt):
        print("Workflow interrupted or local execution failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
