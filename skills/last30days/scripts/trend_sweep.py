#!/usr/bin/env python3
"""Standalone text and YouTube trend sweep (direct CLI; no slash-command integration)."""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import env, trend_sources, trend_sweep, youtube_data  # noqa: E402


def _nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _configured_save_dir():
    """LAST30DAYS_MEMORY_DIR from the process environment or the config files.

    Do not call env.get_config(), which would also open the macOS Keychain
    and pass(1) stores.
    """
    file_env, project_env, _ = env._load_config_files(env.ConfigLoadPolicy())
    return env._configured_memory_dir(
        os.environ.get("LAST30DAYS_MEMORY_DIR"),
        project_env.get("LAST30DAYS_MEMORY_DIR"),
        file_env.get("LAST30DAYS_MEMORY_DIR"),
    )


def _configured_youtube_key():
    """Resolve the optional API key without accessing Keychain or pass."""
    file_env, project_env, _ = env._load_config_files(env.ConfigLoadPolicy())
    for value in (os.environ.get("YOUTUBE_API_KEY"), project_env.get("YOUTUBE_API_KEY"),
                  file_env.get("YOUTUBE_API_KEY")):
        if value and not env.is_unsubstituted_template(value):
            return value.strip()
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sources", help="List sources without network access")
    collect = commands.add_parser("collect", help="Collect and save text sources and optional YouTube")
    collect.add_argument("--save-dir")
    collect.add_argument("--per-source", type=_nonnegative, default=30,
                         help="Summary entries per source; JSON retains all results (default: 30)")
    collect.add_argument("--youtube-searches", type=_nonnegative, default=15)
    collect.add_argument("--no-youtube", action="store_true")
    collect.add_argument("--youtube-topics", default=",".join(youtube_data.DEFAULT_TOPICS))
    args = parser.parse_args(argv)
    if args.command == "sources":
        now = datetime.now(timezone.utc)
        for source in trend_sources.SOURCES:
            print(f"{source.id}\t{source.group}\t{source.format}\t{source.limit}\t{trend_sources.source_url(source, now)}")
        print(f"youtube_chart_kr\tvideo\tyoutube\t50\t{youtube_data.API}videos?chart=mostPopular&regionCode=KR")
        print(f"youtube_search_kr\tvideo\tyoutube\t50/topic\t{youtube_data.API}search")
        return 0
    try:
        location = args.save_dir if args.save_dir is not None else _configured_save_dir()
        if not location or not str(location).strip():
            raise ValueError("Provide --save-dir PATH or configure LAST30DAYS_MEMORY_DIR.")
        directory = Path(location).expanduser()
        # Validate the destination and previous bundle before any source requests.
        directory.mkdir(parents=True, exist_ok=True)
        for path in trend_sweep.delete_expired_youtube(directory):
            print(f"Deleted: {path}")
        trend_sweep.previous_bundle(directory)
        bundle = trend_sweep.collect()
        key = None if args.no_youtube else _configured_youtube_key()
        if not key:
            youtube = {"skipped": "YouTube skipped: " + ("--no-youtube" if args.no_youtube else "YOUTUBE_API_KEY is not configured")}
        else:
            youtube = youtube_data.collect(key, trend_sweep.fetch_url,
                topics=[t.strip() for t in args.youtube_topics.split(",") if t.strip()],
                searches=args.youtube_searches, now=datetime.fromisoformat(bundle["collected_at"]))
        paths = trend_sweep.save_bundle(bundle, directory, per_source=args.per_source, youtube=youtube)
        for source in bundle["sources"]:
            print(f"{source['id']}: {source['fetched']} fetched, {source['kept']} kept")
            if source["id"] in bundle["errors"]:
                error = " ".join(bundle["errors"][source["id"]].split())
                print(f"  ERROR: {error}")
        for section in youtube.get("sections", []):
            source, topic = section["source"], section["topic"]
            count = sum(e["source"] == source and e["extra"]["topic"] == topic for e in youtube["items"])
            label = source + (": " + topic if topic is not None else "")
            print(" ".join(f"{label}: {count} fetched".split()))
        for line in trend_sweep.youtube_status(youtube):
            print(" ".join(line.split()))
        for source, error in youtube.get("errors", {}).items():
            print(" ".join(f"  ERROR {source}: {error}".split()))
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
