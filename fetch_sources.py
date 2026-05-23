"""Fetch one HTTP job source into a snapshot triple.

Usage: python fetch_sources.py <source> [--max N] [--label YYYY-MM-DD]

Writes snapshots/{label}_{source}_{slug}.{json,md,csv} using the same renderers as
fetch.py, so downstream tooling reads new sources exactly like NVIDIA snapshots.
NVIDIA itself stays in fetch.py (Playwright); this drives the plain-HTTP adapters.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import fetch  # reuse render_markdown / render_csv / slugify (Playwright is imported lazily there)
from sources import SOURCES, get_source

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
LOCATION = os.environ.get("MONITOR_LOCATION", "Shanghai, China")


def write_snapshot(source, jobs, label):
    slug = fetch.slugify(LOCATION)
    base = os.path.join(SNAPSHOT_DIR, f"{label}_{source}_{slug}")
    header_location = f"{SOURCES[source]['display']} — {LOCATION}"
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(jobs, ensure_ascii=False, indent=2))
    with open(f"{base}.md", "w", encoding="utf-8") as f:
        f.write(fetch.render_markdown(jobs, header_location, label))
    with open(f"{base}.csv", "w", encoding="utf-8") as f:
        f.write(fetch.render_csv(jobs))
    return base


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch a single HTTP job source into a snapshot")
    parser.add_argument("source", help=f"one of: {', '.join(sorted(SOURCES))}")
    parser.add_argument("--max", type=int, default=None, help="limit number of jobs (for smoke tests)")
    parser.add_argument(
        "--label",
        default=os.environ.get("NVIDIA_SNAPSHOT_LABEL") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    args = parser.parse_args(argv)

    src = get_source(args.source)
    print(f"Fetching {src['display']} ({args.source}) for {LOCATION}...", file=sys.stderr)
    jobs = src["fetch"](max_jobs=args.max)
    base = write_snapshot(args.source, jobs, args.label)
    print(f"Wrote {len(jobs)} jobs -> {base}.{{json,md,csv}}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
