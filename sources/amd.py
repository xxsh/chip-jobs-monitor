"""AMD careers adapter (Phenom platform).

careers.amd.com/api/jobs?city=<city> returns rich job objects with description,
responsibilities, and qualifications inline (HTML) — no separate detail fetch needed.
"""

import urllib.parse

from .base import html_to_items, html_to_text, http_get_json, iso_date, iso_to_ts, normalized_job

API = "https://careers.amd.com/api/jobs"


def _normalize(d):
    summary = html_to_text(d.get("description"))
    responsibilities = html_to_items(d.get("responsibilities"))
    requirements = html_to_items(d.get("qualifications"))
    # Full posting text for the scorer = description + responsibilities + qualifications.
    description = "\n\n".join(
        part for part in (summary, html_to_text(d.get("responsibilities")), html_to_text(d.get("qualifications"))) if part
    )
    req_id = d.get("req_id")
    return normalized_job(
        id=str(req_id) if req_id is not None else None,
        jr=str(req_id) if req_id is not None else None,
        name=d.get("title"),
        locations=[d.get("full_location") or d.get("location_name")],
        department=d.get("department") or None,
        work_location_option=(d.get("location_type") or None),
        posted_ts=iso_to_ts(d.get("posted_date")),
        creation_ts=iso_to_ts(d.get("create_date")),
        date_posted=iso_date(d.get("posted_date")),
        employment_type=d.get("employment_type") or None,
        description=description,
        summary=summary,
        responsibilities=responsibilities,
        requirements=requirements,
        preferred=[],
        link=f"https://careers.amd.com/careers-home/jobs/{req_id}" if req_id is not None else d.get("apply_url"),
    )


def fetch_amd(city="Shanghai", max_jobs=None):
    jobs = []
    page = 1
    limit = 100
    while True:
        query = urllib.parse.urlencode({"city": city, "limit": limit, "page": page})
        data = http_get_json(f"{API}?{query}")
        batch = data.get("jobs") or []
        total = data.get("totalCount") or 0
        for wrapper in batch:
            jobs.append(_normalize(wrapper.get("data") or {}))
        if not batch or len(jobs) >= total:
            break
        page += 1
        if max_jobs and len(jobs) >= max_jobs:
            break
    jobs.sort(key=lambda j: j.get("postedTs") or 0, reverse=True)
    return jobs[:max_jobs] if max_jobs else jobs
