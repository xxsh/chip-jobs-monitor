"""Amazon careers adapter (amazon.jobs JSON search).

GET https://www.amazon.jobs/en/search.json?normalized_country_code[]=CHN&loc_query=Shanghai
returns rich job objects inline (description + basic_qualifications +
preferred_qualifications, all HTML), so no per-job detail fetch is needed.

The site's loc_query filter is fuzzy (a job available in Shanghai *and* Shenzhen
shows up under both searches), so we post-filter on the structured `locations[]`
JSON-string entries — only jobs that list Shanghai as one of their normalized
cities are kept.
"""

import json
import urllib.parse
from datetime import datetime, timezone

from .base import html_to_items, html_to_text, http_get_json, normalized_job

API = "https://www.amazon.jobs/en/search.json"
PAGE_LIMIT = 100  # API returns up to 100 per call


def _parse_posted_date(value):
    """Amazon returns 'May 27, 2026' — convert to (iso_date, ts) tuple."""
    if not value:
        return None, None
    try:
        dt = datetime.strptime(value, "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None, None
    return dt.strftime("%Y-%m-%d"), int(dt.timestamp())


def _in_city(job, city):
    """True if `city` appears as normalizedCityName in any of the job's location entries."""
    target = city.lower()
    for loc_str in job.get("locations") or []:
        try:
            loc = json.loads(loc_str)
        except (TypeError, ValueError):
            continue
        if (loc.get("normalizedCityName") or "").lower() == target:
            return True
    # Fallback: top-level city field on single-city postings.
    return (job.get("city") or "").lower() == target


def _normalize(d):
    icims = d.get("id_icims")
    jid = str(icims) if icims is not None else None

    desc_html = d.get("description") or ""
    basic_html = d.get("basic_qualifications") or ""
    preferred_html = d.get("preferred_qualifications") or ""

    summary = html_to_text(desc_html)
    requirements = html_to_items(basic_html)
    preferred = html_to_items(preferred_html)
    # Full posting text for the scorer = description + basic + preferred.
    description = "\n\n".join(
        part
        for part in (
            summary,
            html_to_text(basic_html),
            html_to_text(preferred_html),
        )
        if part
    )

    date_posted, posted_ts = _parse_posted_date(d.get("posted_date"))

    job_path = d.get("job_path")
    if job_path:
        link = f"https://www.amazon.jobs{job_path}"
    elif jid:
        link = f"https://www.amazon.jobs/en/jobs/{jid}"
    else:
        link = d.get("url_next_step")

    # Build a human-readable locations list from the structured entries.
    location_labels = []
    for loc_str in d.get("locations") or []:
        try:
            loc = json.loads(loc_str)
        except (TypeError, ValueError):
            continue
        city = loc.get("normalizedCityName")
        country = loc.get("countryIso2a") or loc.get("normalizedCountryCode")
        if city:
            location_labels.append(f"{city}, {country}" if country else city)
    if not location_labels and d.get("normalized_location"):
        location_labels = [d["normalized_location"]]

    return normalized_job(
        id=jid,
        jr=jid,
        name=d.get("title"),
        locations=location_labels,
        department=d.get("business_category") or d.get("job_category") or None,
        work_location_option=None,
        posted_ts=posted_ts,
        creation_ts=None,
        date_posted=date_posted,
        employment_type=d.get("job_schedule_type") or None,
        description=description,
        summary=summary,
        responsibilities=[],
        requirements=requirements,
        preferred=preferred,
        link=link,
    )


def fetch_amazon(city="Shanghai", country_code="CHN", max_jobs=None):
    """Fetch all Amazon postings in `city` (post-filtered) within `country_code`.

    `loc_query` narrows the candidate pool server-side (cheaper than paging the
    whole country backlog), then we filter strictly on the structured locations.
    """
    matched = []
    offset = 0
    while True:
        params = urllib.parse.urlencode(
            [
                ("normalized_country_code[]", country_code),
                ("loc_query", city),
                ("result_limit", PAGE_LIMIT),
                ("offset", offset),
                ("sort", "recent"),
            ]
        )
        data = http_get_json(f"{API}?{params}")
        batch = data.get("jobs") or []
        total = int(data.get("hits") or 0)
        for d in batch:
            if _in_city(d, city):
                matched.append(_normalize(d))
        if not batch:
            break
        offset += len(batch)
        if offset >= total or offset > 2000:
            break
        if max_jobs and len(matched) >= max_jobs:
            break

    matched.sort(key=lambda j: j.get("postedTs") or 0, reverse=True)
    return matched[:max_jobs] if max_jobs else matched
