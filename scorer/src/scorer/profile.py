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


def extract_profile(resume_path: Path, *, force: bool = False) -> dict:
    resume_text = resume_path.read_text(encoding="utf-8")
    resume_hash = _hash(resume_text)
    cache_file = _cache_path(resume_hash)

    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text(encoding="utf-8"))

    prompt = PROMPT_TEMPLATE.format(resume=resume_text)
    profile = call_codex(prompt, SCHEMA_PATH, timeout=600)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    # Also write a stable "latest" pointer for daily.py to consume.
    latest = CACHE_DIR / "profile_latest.json"
    latest.write_text(
        json.dumps({"resumeHash": resume_hash, "resumePath": str(resume_path), "profile": profile}, ensure_ascii=False, indent=2),
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
