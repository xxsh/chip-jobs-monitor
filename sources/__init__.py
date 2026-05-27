"""Multi-company job-source registry.

Each source exposes a `fetch(max_jobs=None) -> list[normalized_job]` callable that
returns jobs in the SAME normalized schema as fetch.py (the NVIDIA scraper), so the
existing diff / score / persist / render pipeline works unchanged. NVIDIA stays in
fetch.py (Playwright); these adapters are plain HTTP against public careers APIs.

Registry value:
    {
        "display": <label>,
        "fetch": <callable(max_jobs=None)>,
        "score_filter": <optional callable(job)->bool>,  # if set, only matching jobs go to codex
    }

`score_filter` is a per-source policy hook. Semiconductor sources (NVIDIA, AMD, Intel,
Arm) leave it unset → every fetched job is eligible for scoring. Non-semi sources
(e.g. Amazon) restrict to AI/data-related titles so we don't burn codex budget on the
long tail of irrelevant postings; jobs are still fetched, diffed, snapshotted, and
persisted to MySQL, they just aren't sent to the scorer.
"""

import re

from .amazon import fetch_amazon
from .amd import fetch_amd
from .arm import fetch_arm
from .workday import make_workday_fetcher


# Title-keyword filter for non-semi sources. Broad scope: AI/agent/LLM, data eng,
# data science, ML/applied/research scientist, BI/analytics, and any SDE/SWE title.
_AI_DATA_TITLE_RE = re.compile(
    r"\b("
    r"AI|Agent|LLM|GenAI|"
    r"Data\s+Engineer|Data\s+Scientist|"
    r"Machine\s+Learning|ML\s+Engineer|"
    r"Applied\s+Scientist|Research\s+Scientist|"
    r"BI\s+Engineer|Business\s+Intelligence|Analytics|"
    r"SDE|Software\s+Dev(?:eloper|elopment)?|Software\s+Eng(?:ineer)?"
    r")\b",
    re.IGNORECASE,
)


def ai_data_title_filter(job):
    """True iff the job title looks AI/data/SWE-related. Title-only match (no description)."""
    return bool(_AI_DATA_TITLE_RE.search(job.get("name") or ""))


# Location target is Shanghai across all sources (matches the NVIDIA monitor).
SOURCES = {
    "amazon": {
        "display": "Amazon",
        "fetch": lambda max_jobs=None: fetch_amazon(city="Shanghai", max_jobs=max_jobs),
        "score_filter": ai_data_title_filter,
    },
    "amd": {
        "display": "AMD",
        "fetch": lambda max_jobs=None: fetch_amd(city="Shanghai", max_jobs=max_jobs),
    },
    "arm": {
        "display": "Arm",
        "fetch": lambda max_jobs=None: fetch_arm(max_jobs=max_jobs),
    },
    "intel": {
        "display": "Intel",
        "fetch": make_workday_fetcher(
            host="intel.wd1.myworkdayjobs.com",
            tenant="intel",
            site="External",
            location_keyword="Shanghai",
        ),
    },
}


def get_source(name):
    if name not in SOURCES:
        raise KeyError(f"Unknown source '{name}'. Known: {', '.join(sorted(SOURCES))}")
    return SOURCES[name]


def score_filter_for(source):
    """Look up the score_filter for a source name; returns None if the source has no filter
    (and for unknown sources like 'nvidia' which lives in fetch.py)."""
    entry = SOURCES.get(source)
    return entry.get("score_filter") if entry else None
