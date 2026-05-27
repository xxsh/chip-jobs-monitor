"""Multi-company job-source registry.

Each source exposes a `fetch(max_jobs=None) -> list[normalized_job]` callable that
returns jobs in the SAME normalized schema as fetch.py (the NVIDIA scraper), so the
existing diff / score / persist / render pipeline works unchanged. NVIDIA stays in
fetch.py (Playwright); these adapters are plain HTTP against public careers APIs.

Registry value: {"display": <label>, "fetch": <callable(max_jobs=None)>}.
"""

from .amazon import fetch_amazon
from .amd import fetch_amd
from .arm import fetch_arm
from .workday import make_workday_fetcher

# Location target is Shanghai across all sources (matches the NVIDIA monitor).
SOURCES = {
    "amazon": {
        "display": "Amazon",
        "fetch": lambda max_jobs=None: fetch_amazon(city="Shanghai", max_jobs=max_jobs),
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
