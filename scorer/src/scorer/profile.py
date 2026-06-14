"""Extract a structured candidate profile from a resume markdown file.

Cached by SHA-256 of resume content so we only call codex when the
resume actually changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from scorer.llm import LLMError, call_codex

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).parent / "schemas" / "profile.schema.json"
CACHE_DIR = ROOT / "cache"

PROMPT_TEMPLATE = """\
You are reading a candidate's resume and extracting a structured profile
that will be used to score job-resume fit.

Return a JSON object matching the schema you have been given. Be specific
and concrete — pull actual technologies, domains, and seniority signals
from the resume, not generic phrases. The profile must be honest about
both strengths and likely poor-fit areas.

Every array item must be a single atomic value — one skill, one domain, one
degree per string. Never concatenate several into one entry; split on commas.

For `education`, list each formal degree as its own entry with degree level,
field/major, institution, and years. Many postings gate on a specific degree
(e.g. "MS in CS/EE or a related field"), so capture the level and field
faithfully — do not omit or upgrade them.

For `antiPreferences`, infer roles the candidate would NOT be a strong
match for based on what is missing from the resume (e.g., if there is no
RTL/Verilog experience, "RTL or physical ASIC design" is an
anti-preference).

For `headline`, write one tight sentence the candidate could put at the
top of their resume.

<resume>
{resume}
</resume>
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cache_path(resume_hash: str) -> Path:
    return CACHE_DIR / f"profile_{resume_hash}.json"


# Profile fields that actually drive job-resume scoring. `headline` is a
# cosmetic one-liner and is deliberately excluded, so a reword-only resume
# edit that re-extracts to the same signal keeps the same profile_hash and
# does not invalidate every previously computed score.
_HASH_SCALAR_FIELDS = ("level", "yearsExperience")
_HASH_LIST_FIELDS = (
    "primaryDomains",
    "coreSkills",
    "secondarySkills",
    "preferences",
    "antiPreferences",
    "education",
)


def _canonical_profile(profile: dict) -> dict:
    """Normalized view of the scoring-relevant signal, stable across
    LLM reordering/casing of list items."""
    canonical: dict = {}
    for key in _HASH_SCALAR_FIELDS:
        value = profile.get(key)
        canonical[key] = value.strip().casefold() if isinstance(value, str) else value
    for key in _HASH_LIST_FIELDS:
        items = profile.get(key) or []
        canonical[key] = sorted({str(x).strip().casefold() for x in items if str(x).strip()})
    return canonical


def profile_hash(profile: dict) -> str:
    """Stable identity for a profile's scoring signal.

    Derived from the *extracted profile content*, not the resume bytes, so
    cosmetic resume edits that re-extract to the same signal keep the same
    hash — and therefore don't invalidate previously computed scores. This is
    the value written as `resumeHash` and consumed everywhere as the profile
    identity (db.py, the dashboard, the rescore importer, daily.py).
    """
    canonical = json.dumps(_canonical_profile(profile), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def extract_profile(resume_path: Path, *, force: bool = False) -> dict:
    resume_text = resume_path.read_text(encoding="utf-8")
    text_hash = _hash(resume_text)
    cache_file = _cache_path(text_hash)

    if cache_file.exists() and not force:
        profile = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        prompt = PROMPT_TEMPLATE.format(resume=resume_text)
        profile = call_codex(prompt, SCHEMA_PATH, timeout=600)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    # Always refresh the "latest" pointer daily.py/db.py consume. `resumeHash`
    # is the profile-content hash (scoring identity), NOT the resume-text hash,
    # so cosmetic edits don't churn it; `resumeTextHash` records the source bytes.
    latest = CACHE_DIR / "profile_latest.json"
    latest.write_text(
        json.dumps(
            {
                "resumeHash": profile_hash(profile),
                "resumeTextHash": text_hash,
                "resumePath": str(resume_path),
                "profile": profile,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract structured profile from resume")
    parser.add_argument("--resume", required=True, type=Path, help="Path to resume markdown")
    parser.add_argument("--force", action="store_true", help="Re-run even if cached")
    args = parser.parse_args(argv)

    if not args.resume.exists():
        print(f"resume not found: {args.resume}", file=sys.stderr)
        return 2

    try:
        profile = extract_profile(args.resume, force=args.force)
    except LLMError as exc:
        print(f"profile extraction failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(profile, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
