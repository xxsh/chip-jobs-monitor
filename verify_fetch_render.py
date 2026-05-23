"""Offline equivalence check for the fetch.py port.

For every NVIDIA golden snapshot triple, confirm that fetch.py reproduces
byte-identical JSON, Markdown, and CSV from the same data. This isolates the
deterministic rendering/serialization layer from the network. Multi-company
snapshots (sources/ adapters, `_{source}_` infix) are skipped — fetch.py does
not produce them.

Usage: python verify_fetch_render.py
Exit code 0 = all match, 1 = at least one mismatch.
"""

import glob
import json
import os
import re
import sys

import fetch

SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")

# fetch.py only produces NVIDIA snapshots (bare `{date}_{location-slug}`). Multi-company
# adapters in sources/ write a `_{source}_` infix and are parsed by their own adapters, not
# fetch.py — so they are excluded from this NVIDIA-parser equivalence check.
NVIDIA_SNAP_RE = re.compile(r"\d{4}-\d{2}-\d{2}_[a-z-]+$")

# Golden header line: "# NVIDIA jobs — {location} — {label}"
HEADER_RE = re.compile(r"^# NVIDIA jobs — (.+?) — (.+?)\n")


def first_diff(a, b):
    """Return a human-readable description of the first difference, or None."""
    if a == b:
        return None
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            ctx_lo = max(0, i - 40)
            return (
                f"byte {i}: got {a[i - ctx_lo : i + 40]!r} "
                f"vs golden {b[i - ctx_lo : i + 40]!r}"
            )
    return f"length differs: got {len(a)} vs golden {len(b)}"


def check(base):
    name = os.path.basename(base)
    golden_json = open(f"{base}.json", encoding="utf-8").read()
    golden_md = open(f"{base}.md", encoding="utf-8").read()
    golden_csv = open(f"{base}.csv", encoding="utf-8").read()

    jobs = json.loads(golden_json)

    header = HEADER_RE.match(golden_md)
    if not header:
        return [f"{name}: could not parse md header"]
    location, label = header.group(1), header.group(2)

    got_json = json.dumps(jobs, ensure_ascii=False, indent=2)
    got_md = fetch.render_markdown(jobs, location, label)
    got_csv = fetch.render_csv(jobs)

    failures = []
    for kind, got, golden in (
        ("json", got_json, golden_json),
        ("md", got_md, golden_md),
        ("csv", got_csv, golden_csv),
    ):
        diff = first_diff(got, golden)
        if diff:
            failures.append(f"{name}.{kind}: {diff}")

    # Re-parse each raw description and confirm the parser reproduces the stored
    # section fields (exercises parse_description_sections / clean_text on real data).
    for job in jobs:
        if job.get("detailError") or not job.get("description"):
            continue
        parsed = fetch.parse_description_sections(job["description"])
        for field in ("summary", "responsibilities", "requirements", "preferred"):
            if parsed[field] != job.get(field):
                failures.append(f"{name} [{job.get('jr')}] parse.{field} differs")
    return failures


def main():
    bases = sorted(
        b[: -len(".json")]
        for b in glob.glob(os.path.join(SNAP_DIR, "*.json"))
        if NVIDIA_SNAP_RE.fullmatch(os.path.basename(b)[: -len(".json")])
        and os.path.exists(b[: -len(".json")] + ".md")
        and os.path.exists(b[: -len(".json")] + ".csv")
    )
    if not bases:
        print("No golden triples found", file=sys.stderr)
        return 1

    all_failures = []
    for base in bases:
        all_failures.extend(check(base))

    checked = len(bases)
    if all_failures:
        print(f"FAIL — {len(all_failures)} mismatch(es) across {checked} snapshots:")
        for f in all_failures:
            print(f"  {f}")
        return 1

    print(f"OK — json/md/csv byte-identical for all {checked} golden snapshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
