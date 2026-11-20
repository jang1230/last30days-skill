#!/usr/bin/env python3
"""Direct CLI / dev tool for a standalone Korea gap check."""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import dates, env, korea_gap, naver, private_files  # noqa: E402

BUNDLE_FILENAME = "korea-gap-candidates.json"
KST = timezone(timedelta(hours=9))


def _korea_today(now=None):
    """Naver post dates are Korean calendar days, so the check window ends on today's date in Korea."""
    return (now or datetime.now(timezone.utc)).astimezone(KST).date().isoformat()


def _nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _save_report(directory, to_date, report):
    """Reserve an unused filename, then atomically publish owner-only text."""
    suffix = 1
    while True:
        tail = "" if suffix == 1 else f"-{suffix}"
        path = directory / f"korea-gap-{to_date}{tail}.md"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        os.close(fd)
        try:
            private_files.replace_private_text(path, report)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path


def _load_bundle(path):
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(bundle, dict) or bundle.get("schema") != 1
                or not isinstance(bundle.get("bundle_id"), str) or not bundle["bundle_id"]
                or not isinstance(bundle.get("overseas"), list)
                or not isinstance(bundle.get("korea"), list)
                or not isinstance(bundle.get("errors"), dict)):
            raise ValueError("invalid bundle shape")
        return bundle
    except (OSError, ValueError) as exc:
        raise korea_gap.PicksError("Cannot read candidates bundle; run collect with the same --save-dir first.") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--days", type=_nonnegative, default=7)
    collect.add_argument("--min-points", type=_nonnegative, default=50)
    collect.add_argument("--save-dir")
    collect.add_argument("--emit", choices=("text", "json"), default="text")
    check = commands.add_parser("check")
    check.add_argument("--picks", required=True)
    check.add_argument("--naver-budget", type=_nonnegative, default=40)
    check.add_argument("--save-dir")
    check.add_argument("--emit", choices=("md", "json"), default="md")
    args = parser.parse_args(argv)
    try:
        config = env.get_config()
        location = args.save_dir if args.save_dir is not None else config.get("LAST30DAYS_MEMORY_DIR")
        if not location or not str(location).strip():
            raise korea_gap.PicksError("Provide --save-dir PATH or configure LAST30DAYS_MEMORY_DIR.")
        directory = Path(location).expanduser()
        bundle_path = directory / BUNDLE_FILENAME
        if args.command == "collect":
            directory.mkdir(parents=True, exist_ok=True)
            from_date, to_date = dates.get_date_range(args.days)
            bundle = korea_gap.collect(from_date=from_date, to_date=to_date, min_points=args.min_points)
            private_files.replace_private_text(bundle_path, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
            print(f"Saved bundle: {bundle_path}", file=sys.stderr)
            print(json.dumps(bundle, ensure_ascii=False, indent=2) if args.emit == "json"
                  else korea_gap.render_digest(bundle, bundle_path), end="\n")
            return 1 if len(bundle["errors"]) == 3 else 0
        bundle = _load_bundle(bundle_path)
        picks = korea_gap.load_picks(args.picks, bundle)
        credentials = naver.resolve_credentials(config)
        if credentials is None:
            raise korea_gap.PicksError(naver.count_mentions("", "", "", credentials=None)["errors"][0])
        from_date, to_date = dates.get_date_range(30, as_of_date=_korea_today())
        result = korea_gap.check(bundle, picks, credentials=credentials,
                                 from_date=from_date, to_date=to_date, budget=args.naver_budget)
        report = korea_gap.render_report(bundle, result)
        report_path = _save_report(directory, to_date, report)
        print(f"Saved report: {report_path}", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.emit == "json" else report,
              end="\n" if args.emit == "json" else "")
        return 0
    except (korea_gap.PicksError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _utf8_output():
    """The digest and report are Korean: a legacy stream encoding must not fail them."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _utf8_output()
    raise SystemExit(main())
