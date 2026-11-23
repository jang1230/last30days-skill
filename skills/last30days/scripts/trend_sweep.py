#!/usr/bin/env python3
"""Standalone text trend sweep (direct CLI; no slash-command integration)."""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import env, trend_sources, trend_sweep  # noqa: E402


def _nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _configured_save_dir():
    """LAST30DAYS_MEMORY_DIR from the process environment or the config files.

    The sweep needs no credentials, so it does not call env.get_config(), which
    would also open the macOS Keychain and pass(1) stores.
    """
    file_env, project_env, _ = env._load_config_files(env.ConfigLoadPolicy())
    return env._configured_memory_dir(
        os.environ.get("LAST30DAYS_MEMORY_DIR"),
        project_env.get("LAST30DAYS_MEMORY_DIR"),
        file_env.get("LAST30DAYS_MEMORY_DIR"),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sources", help="List sources without network access")
    collect = commands.add_parser("collect", help="Collect and save text sources")
    collect.add_argument("--save-dir")
    collect.add_argument("--per-source", type=_nonnegative, default=30,
                         help="Summary entries per source; JSON retains all results (default: 30)")
    args = parser.parse_args(argv)
    if args.command == "sources":
        now = datetime.now(timezone.utc)
        for source in trend_sources.SOURCES:
            print(f"{source.id}\t{source.group}\t{source.format}\t{source.limit}\t{trend_sources.source_url(source, now)}")
        return 0
    try:
        location = args.save_dir if args.save_dir is not None else _configured_save_dir()
        if not location or not str(location).strip():
            raise ValueError("Provide --save-dir PATH or configure LAST30DAYS_MEMORY_DIR.")
        directory = Path(location).expanduser()
        # Validate the destination and previous bundle before any source requests.
        directory.mkdir(parents=True, exist_ok=True)
        trend_sweep.previous_bundle(directory)
        bundle = trend_sweep.collect()
        paths = trend_sweep.save_bundle(bundle, directory, per_source=args.per_source)
        for source in bundle["sources"]:
            print(f"{source['id']}: {source['fetched']} fetched, {source['kept']} kept")
            if source["id"] in bundle["errors"]:
                error = " ".join(bundle["errors"][source["id"]].split())
                print(f"  ERROR: {error}")
        for path in paths.values():
            print(f"Saved: {path}")
        return 1 if len(bundle["errors"]) == len(bundle["sources"]) else 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _utf8_output():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _utf8_output()
    raise SystemExit(main())
