"""Generic Workday adapter (the *.myworkdayjobs.com CXS API).

One implementation covers any Workday tenant (Intel, NXP, ADI, onsemi, ...).
Listing: POST /wday/cxs/{tenant}/{site}/jobs  {appliedFacets,limit,offset,searchText}
Detail:  GET  /wday/cxs/{tenant}/{site}{externalPath} -> {jobPostingInfo:{jobDescription,...}}

Location is filtered by searchText=<keyword> then post-filtered on locationsText, so we
don't need to discover per-tenant location facet IDs.
"""

from concurrent.futures import ThreadPoolExecutor

from .base import html_to_text, http_get_json, http_post_json, iso_date, normalized_job

PAGE_LIMIT = 20  # Workday caps page size at 20


def _normalize(cxs_base, host, posting, detail_concurrency_ok=True):
    path = posting.get("externalPath")
    bullet = posting.get("bulletFields") or []
    jr = bullet[0] if bullet else None
    title = posting.get("title")
    locations = [posting.get("locationsText")]
    link = f"https://{host}{path}" if path else None

    description = ""
    date_posted = None
    employment_type = None
    detail_error = None
    if path:
        try:
            info = http_get_json(f"{cxs_base}{path}").get("jobPostingInfo") or {}
            description = html_to_text(info.get("jobDescription"))
            date_posted = iso_date(info.get("startDate"))
            employment_type = info.get("timeType")
            link = info.get("externalUrl") or link
            if not jr:
                jr = info.get("jobReqId")
        except Exception as exc:  # noqa: BLE001 - record per-job detail failures, keep going
            detail_error = str(exc)

    return normalized_job(
        id=jr,
        jr=jr,
        name=title,
        locations=locations,
        department=None,
        date_posted=date_posted,
        employment_type=employment_type,
        description=description,
        summary=description,
        responsibilities=[],
        requirements=[],
        preferred=[],
        link=link,
        detail_error=detail_error,
    )


def make_workday_fetcher(*, host, tenant, site, location_keyword="Shanghai", detail_concurrency=6):
    cxs_base = f"https://{host}/wday/cxs/{tenant}/{site}"

    def fetch(max_jobs=None):
        matched = []
        offset = 0
        while True:
            data = http_post_json(
                f"{cxs_base}/jobs",
                {"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": offset, "searchText": location_keyword},
            )
            postings = data.get("jobPostings") or []
            total = data.get("total") or 0
            for p in postings:
                loc = (p.get("locationsText") or "").lower()
                if location_keyword.lower() in loc:
                    matched.append(p)
            offset += len(postings)
            if not postings or offset >= total or offset > 2000:
                break
            if max_jobs and len(matched) >= max_jobs:
                break

        if max_jobs:
            matched = matched[:max_jobs]

        workers = max(1, min(detail_concurrency, len(matched) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            jobs = list(executor.map(lambda p: _normalize(cxs_base, host, p), matched))
        jobs.sort(key=lambda j: j.get("datePosted") or "", reverse=True)
        return jobs

    return fetch
