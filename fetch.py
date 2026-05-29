"""Scrape NVIDIA's job site for a location and write a snapshot triple.

Python port of fetch.mjs. Produces byte-identical snapshots/{label}_{slug}.{json,md,csv}.
Playwright is imported lazily inside create_context so the pure functions
(rendering, parsing, diffing) can be imported and tested without a browser.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote

__dirname = os.path.dirname(os.path.abspath(__file__))


def parse_positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


LOCATION = os.environ.get("NVIDIA_LOCATION") or "Shanghai, China"
SNAPSHOT_DIR = os.path.join(__dirname, "snapshots")
# By default use Playwright's bundled chromium (installed via `playwright install
# chromium`). Set NVIDIA_CHROMIUM_PATH to point at a specific browser / headless-shell
# binary only if you need to override it.
CHROMIUM_PATH = os.environ.get("NVIDIA_CHROMIUM_PATH") or None
SNAPSHOT_LABEL = os.environ.get("NVIDIA_SNAPSHOT_LABEL") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
MAX_JOBS = parse_positive_int(os.environ.get("NVIDIA_MAX_JOBS"))
DETAIL_CONCURRENCY = parse_positive_int(os.environ.get("NVIDIA_DETAIL_CONCURRENCY")) or 6

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# encodeURIComponent leaves A-Za-z0-9 and -_.!~*'() unescaped; quote always keeps -_.~
_URI_SAFE = "!*'()"


def is_date_label(value):
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))


def slugify(value):
    s = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return re.sub(r"^-|-$", "", s)


def job_key(job):
    if job.get("id") is not None:
        return str(job["id"])
    if job.get("jr") is not None:
        return str(job["jr"])
    name = job.get("name")
    name = "" if name is None else name
    posted = job.get("postedTs")
    if posted is None:
        posted = job.get("datePosted")
    if posted is None:
        posted = ""
    return f"{name}|{posted}"


def job_metadata_signature(job):
    return json.dumps(
        {
            "id": job.get("id"),
            "jr": job.get("jr"),
            "name": job.get("name"),
            "postedTs": job.get("postedTs"),
            "locations": job.get("locations") or [],
            "department": job.get("department"),
            "workLocationOption": job.get("workLocationOption"),
        },
        ensure_ascii=False,
    )


def snapshot_suffix():
    return f"_{slugify(LOCATION)}.json"


def find_previous_snapshot_file(current_label=None):
    if current_label is None:
        current_label = SNAPSHOT_LABEL
    suffix = snapshot_suffix()
    files = []
    for fn in os.listdir(SNAPSHOT_DIR):
        if not fn.endswith(suffix):
            continue
        if fn.startswith(current_label):
            continue
        if is_date_label(current_label):
            label = fn[: -len(suffix)]
            if not is_date_label(label):
                continue
        files.append(fn)
    files.sort()
    return os.path.join(SNAPSHOT_DIR, files[-1]) if files else None


def load_snapshot(file):
    with open(file, "r", encoding="utf-8") as f:
        return json.load(f)


def can_reuse_details(job):
    if not job or job.get("detailError"):
        return False
    return bool(
        job.get("description")
        or job.get("summary")
        or job.get("datePosted")
        or job.get("validThrough")
        or job.get("employmentType")
        or (job.get("responsibilities") or [])
        or (job.get("requirements") or [])
        or (job.get("preferred") or [])
    )


def copy_reusable_details(job, previous_job):
    return {
        **job,
        "datePosted": previous_job.get("datePosted") or None,
        "validThrough": previous_job.get("validThrough") or None,
        "employmentType": previous_job.get("employmentType") or None,
        "description": previous_job.get("description") or "",
        "summary": previous_job.get("summary") or "",
        "responsibilities": previous_job.get("responsibilities") or [],
        "requirements": previous_job.get("requirements") or [],
        "preferred": previous_job.get("preferred") or [],
        "detailError": previous_job.get("detailError") or None,
    }


def plan_detail_enrichment(current_jobs, previous_jobs):
    previous_by_key = {job_key(job): job for job in previous_jobs}
    planned_jobs = [None] * len(current_jobs)
    pending_jobs = []
    reused_count = 0

    for index, job in enumerate(current_jobs):
        previous_job = previous_by_key.get(job_key(job))
        same_metadata = previous_job is not None and job_metadata_signature(previous_job) == job_metadata_signature(job)
        if same_metadata and can_reuse_details(previous_job):
            planned_jobs[index] = copy_reusable_details(job, previous_job)
            reused_count += 1
            continue
        pending_jobs.append({"index": index, "job": job})

    return {"plannedJobs": planned_jobs, "pendingJobs": pending_jobs, "reusedCount": reused_count}


def apply_enriched_jobs(planned_jobs, pending_jobs, enriched_jobs):
    merged = list(planned_jobs)
    for i, item in enumerate(pending_jobs):
        merged[item["index"]] = enriched_jobs[i]
    return merged


def normalize(jobs):
    needle = LOCATION.split(",")[0].lower()
    filtered = [
        job
        for job in jobs
        if any(needle in (loc or "").lower() for loc in (job.get("locations") or []))
    ]
    mapped = [
        {
            "id": job.get("id"),
            "jr": job.get("displayJobId"),
            "name": job.get("name"),
            "locations": job.get("locations"),
            "department": job.get("department"),
            "workLocationOption": job.get("workLocationOption"),
            "postedTs": job.get("postedTs"),
            "creationTs": job.get("creationTs"),
            "link": f"https://jobs.nvidia.com/careers/job/{job.get('id')}",
        }
        for job in filtered
    ]
    mapped.sort(key=lambda job: job.get("postedTs") or 0, reverse=True)
    return mapped[:MAX_JOBS] if MAX_JOBS else mapped


def clean_text(text):
    s = "" if text is None else str(text)
    s = s.replace("\r", "")
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def to_one_line(text):
    return re.sub(r"\s+", " ", clean_text(text)).strip()


def strip_bullet(line):
    return re.sub(r"^[*-]\s+", "", line, count=1).strip()


_BOILERPLATE = [
    re.compile(r"widely considered to be one of the technology world.?s most desirable employers", re.I),
    re.compile(r"as you plan your future, see what we can offer", re.I),
    re.compile(r"www\.nvidiabenefits\.com", re.I),
    re.compile(r"^if you(?:'|’)re creative and autonomous, we want to hear from you!?$", re.I),
]


def is_boilerplate_line(line):
    return any(pattern.search(line) for pattern in _BOILERPLATE)


def parse_description_sections(description):
    lines = [ln.strip() for ln in clean_text(description).split("\n")]
    lines = [ln for ln in lines if ln and ln != "##"]

    sections = {"summary": [], "responsibilities": [], "requirements": [], "preferred": []}
    current = "summary"
    for line in lines:
        if re.match(r"^what you(?:'|’)ll be doing:?$", line, re.I):
            current = "responsibilities"
            continue
        if re.match(r"^what we need to see:?$", line, re.I):
            current = "requirements"
            continue
        if re.match(r"^ways to stand out from the crowd:?$", line, re.I):
            current = "preferred"
            continue
        if is_boilerplate_line(line):
            continue
        sections[current].append(line if current == "summary" else strip_bullet(line))

    return {
        "summary": "\n\n".join(sections["summary"]).strip(),
        "responsibilities": [x for x in sections["responsibilities"] if x],
        "requirements": [x for x in sections["requirements"] if x],
        "preferred": [x for x in sections["preferred"] if x],
    }


def extract_job_posting(html):
    for match in re.finditer(
        r'<script type="application/ld\+json">\s*({.*?})\s*</script>', html, re.I | re.S
    ):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("@type") == "JobPosting":
            return parsed
    return None


def enrich_job(job, posting):
    if not posting:
        return {**job, "detailError": "Missing JobPosting structured data"}

    description = clean_text(posting.get("description"))
    sections = parse_description_sections(description)

    return {
        **job,
        "datePosted": posting.get("datePosted") or None,
        "validThrough": posting.get("validThrough") or None,
        "employmentType": posting.get("employmentType") or None,
        "description": description,
        "summary": sections["summary"],
        "responsibilities": sections["responsibilities"],
        "requirements": sections["requirements"],
        "preferred": sections["preferred"],
        "detailError": None,
    }


def _fmt_ts(value):
    if not value:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_iso(value):
    if not value:
        return ""
    return str(value)[:10]


def render_section(title, items):
    if not items:
        return ""
    output = f"\n#### {title}\n\n"
    for item in items:
        output += f"- {item}\n"
    return output


def render_markdown(jobs, location, label):
    md = (
        f"# NVIDIA jobs — {location} — {label}\n\n"
        f"{len(jobs)} jobs\n\n"
        "| # | JR ID | Title | Locations | Dept | Mode | Posted | Link |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    for index, job in enumerate(jobs):
        name = str(job.get("name")).replace("|", "\\|")
        md += (
            f"| {index + 1} | {job.get('jr')} | {name} | "
            f"{'; '.join(job.get('locations') or [])} | {job.get('department') or ''} | "
            f"{job.get('workLocationOption') or ''} | {_fmt_ts(job.get('postedTs'))} | "
            f"[link]({job.get('link')}) |\n"
        )

    md += "\n## Details\n"
    for index, job in enumerate(jobs):
        md += f"\n### {index + 1}. {job.get('name')} ({job.get('jr')})\n\n"
        md += f"- Locations: {'; '.join(job.get('locations') or [])}\n"
        md += f"- Department: {job.get('department') or ''}\n"
        md += f"- Work mode: {job.get('workLocationOption') or ''}\n"
        md += f"- Posted: {_fmt_ts(job.get('postedTs'))}\n"
        md += f"- Date posted (detail): {_fmt_iso(job.get('datePosted'))}\n"
        md += f"- Valid through: {_fmt_iso(job.get('validThrough'))}\n"
        md += f"- Employment type: {job.get('employmentType') or ''}\n"
        md += f"- Link: {job.get('link')}\n"
        if job.get("detailError"):
            md += f"- Detail fetch error: {job.get('detailError')}\n"
            continue
        if job.get("summary"):
            md += f"\n{job.get('summary')}\n"
        md += render_section("Responsibilities", job.get("responsibilities"))
        md += render_section("Requirements", job.get("requirements"))
        md += render_section("Preferred", job.get("preferred"))
    return md


def _csv_escape(value):
    if value is None:
        return '""'
    return '"' + str(value).replace('"', '""') + '"'


def render_csv(jobs):
    csv = (
        "jr_id,title,locations,department,work_mode,posted,link,date_posted,"
        "valid_through,employment_type,summary,responsibilities,requirements,"
        "preferred,detail_error\n"
    )
    for job in jobs:
        fields = [
            job.get("jr"),
            job.get("name"),
            "; ".join(job.get("locations") or []),
            job.get("department"),
            job.get("workLocationOption"),
            _fmt_ts(job.get("postedTs")),
            job.get("link"),
            _fmt_iso(job.get("datePosted")),
            _fmt_iso(job.get("validThrough")),
            job.get("employmentType"),
            to_one_line(job.get("summary")),
            " | ".join(job.get("responsibilities") or []),
            " | ".join(job.get("requirements") or []),
            " | ".join(job.get("preferred") or []),
            job.get("detailError"),
        ]
        csv += ",".join(_csv_escape(f) for f in fields) + "\n"
    return csv


def write_snapshot(jobs):
    base = os.path.join(SNAPSHOT_DIR, f"{SNAPSHOT_LABEL}_{slugify(LOCATION)}")
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(jobs, ensure_ascii=False, indent=2))
    with open(f"{base}.md", "w", encoding="utf-8") as f:
        f.write(render_markdown(jobs, LOCATION, SNAPSHOT_LABEL))
    with open(f"{base}.csv", "w", encoding="utf-8") as f:
        f.write(render_csv(jobs))
    return base


def diff_with_previous(current):
    previous_file = find_previous_snapshot_file(SNAPSHOT_LABEL)
    if not previous_file:
        return None
    previous_jobs = load_snapshot(previous_file)
    previous_keys = {job_key(job) for job in previous_jobs}
    current_keys = {job_key(job) for job in current}
    added = [job for job in current if job_key(job) not in previous_keys]
    removed = [job for job in previous_jobs if job_key(job) not in current_keys]
    return {
        "prevFile": os.path.basename(previous_file),
        "prevCount": len(previous_jobs),
        "added": added,
        "removed": removed,
    }


# --- Playwright-backed scraping (browser imported lazily) ---

# jobs.nvidia.com is an Eightfold SPA that pulls ~MB of app JS before the page
# fires domcontentloaded. Over a throttled link that overran the 60s nav timeout
# and broke the NVIDIA fetch outright (observed 2026-05-29). We never need the
# rendered SPA — only the document itself (it sets the session cookie, and detail
# pages carry the posting as inline ld+json) plus the in-page pcsx API call. So
# abort the heavy subresources: navigation settles in seconds and the connection
# stays free for the API fetch. Routing only blocks network requests, so an inline
# <script type="application/ld+json"> on a detail page is unaffected.
_BLOCK_RESOURCE_TYPES = {"script", "stylesheet", "image", "font", "media"}


async def _block_heavy_assets(route):
    try:
        if route.request.resource_type in _BLOCK_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:  # noqa: BLE001 - routing races during teardown are non-fatal
        pass


async def create_context():
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    launch_kwargs = {"headless": True}
    if CHROMIUM_PATH:
        launch_kwargs["executable_path"] = CHROMIUM_PATH
    browser = await pw.chromium.launch(**launch_kwargs)
    ctx = await browser.new_context(user_agent=USER_AGENT)
    await ctx.route("**/*", _block_heavy_assets)
    return pw, browser, ctx


async def fetch_all(ctx):
    page = await ctx.new_page()

    prime_url = f"https://jobs.nvidia.com/careers?location={quote(LOCATION, safe=_URI_SAFE)}&sort_by=timestamp"
    await page.goto(prime_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)

    all_positions = []
    start = 0
    step = 10
    while True:
        url = (
            "https://jobs.nvidia.com/api/pcsx/search?domain=nvidia.com&query=&location="
            f"{quote(LOCATION, safe=_URI_SAFE)}&start={start}&num={step}&sort_by=timestamp&"
        )
        res = await page.evaluate(
            """async (targetUrl) => {
                const response = await fetch(targetUrl, { credentials: 'include', headers: { Accept: 'application/json' } });
                return { status: response.status, text: await response.text() };
            }""",
            url,
        )
        if res["status"] != 200:
            sys.stderr.write(f"ERR status {res['status']} {res['text'][:200]}\n")
            break
        payload = json.loads(res["text"])
        data = payload.get("data") or {}
        positions = data.get("positions") or []
        reported_total = data.get("count") or 0
        all_positions.extend(positions)
        sys.stderr.write(f"  fetched {len(all_positions)}/{reported_total}\r")
        sys.stderr.flush()
        if len(positions) == 0:
            break
        start += len(positions)
        if reported_total and start >= reported_total:
            break
        if start > 2000:
            break
    sys.stderr.write("\n")

    seen = set()
    unique = []
    for position in all_positions:
        if position["id"] in seen:
            continue
        seen.add(position["id"])
        unique.append(position)
    await page.close()
    return unique


async def fetch_job_detail_html(page, url):
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if not response or not response.ok:
        status = response.status if response else "unknown"
        raise RuntimeError(f"HTTP {status}")
    return await page.content()


async def enrich_jobs(ctx, jobs):
    if not jobs:
        return jobs

    enriched = [None] * len(jobs)
    state = {"next": 0, "completed": 0}

    async def worker():
        page = await ctx.new_page()
        try:
            while True:
                current_index = state["next"]
                state["next"] += 1
                if current_index >= len(jobs):
                    return

                job = jobs[current_index]
                try:
                    html = await fetch_job_detail_html(page, job["link"])
                    posting = extract_job_posting(html)
                    enriched[current_index] = enrich_job(job, posting)
                except Exception as error:  # noqa: BLE001 - mirror JS catch-all
                    enriched[current_index] = {**job, "detailError": str(error)}

                state["completed"] += 1
                sys.stderr.write(f"  fetched details {state['completed']}/{len(jobs)}\r")
                sys.stderr.flush()
        finally:
            await page.close()

    worker_count = min(DETAIL_CONCURRENCY, len(jobs))
    await asyncio.gather(*[worker() for _ in range(worker_count)])
    sys.stderr.write("\n")
    return enriched


async def main():
    print(f'Fetching NVIDIA jobs for "{LOCATION}"...', file=sys.stderr)
    if MAX_JOBS:
        print(f"Limiting snapshot to first {MAX_JOBS} jobs via NVIDIA_MAX_JOBS.", file=sys.stderr)

    pw, browser, ctx = await create_context()
    try:
        raw = await fetch_all(ctx)
        jobs = normalize(raw)
        previous_snapshot_file = find_previous_snapshot_file(SNAPSHOT_LABEL)
        previous_jobs = load_snapshot(previous_snapshot_file) if previous_snapshot_file else []
        plan = plan_detail_enrichment(jobs, previous_jobs)

        if previous_snapshot_file:
            print(
                f"Compared against {os.path.basename(previous_snapshot_file)} using lightweight listing metadata.",
                file=sys.stderr,
            )
            print(
                f"Reusing details for {plan['reusedCount']} unchanged jobs; fetching details for "
                f"{len(plan['pendingJobs'])} new or changed jobs with concurrency {DETAIL_CONCURRENCY}...",
                file=sys.stderr,
            )
        else:
            print(
                f"No previous snapshot found; fetching details for all {len(jobs)} jobs "
                f"with concurrency {DETAIL_CONCURRENCY}...",
                file=sys.stderr,
            )

        enriched_pending = await enrich_jobs(ctx, [item["job"] for item in plan["pendingJobs"]])
        enriched_jobs = apply_enriched_jobs(plan["plannedJobs"], plan["pendingJobs"], enriched_pending)
        base = write_snapshot(enriched_jobs)
        print(f"Wrote {len(enriched_jobs)} jobs -> {base}.{{md,csv,json}}", file=sys.stderr)

        if MAX_JOBS:
            print("Skipping diff because NVIDIA_MAX_JOBS produced a partial snapshot.", file=sys.stderr)
            return

        diff = diff_with_previous(enriched_jobs)
        if not diff:
            print("No previous snapshot to diff against.", file=sys.stderr)
            return
        print(f"\nDiff vs {diff['prevFile']} ({diff['prevCount']} jobs):", file=sys.stderr)
        print(f"  + {len(diff['added'])} new", file=sys.stderr)
        print(f"  - {len(diff['removed'])} removed", file=sys.stderr)
        if diff["added"]:
            print("\nNEW:", file=sys.stderr)
            for job in diff["added"]:
                print(f"  + [{job['jr']}] {job['name']} — {'; '.join(job['locations'])}", file=sys.stderr)
        if diff["removed"]:
            print("\nREMOVED:", file=sys.stderr)
            for job in diff["removed"]:
                print(f"  - [{job['jr']}] {job['name']}", file=sys.stderr)
    finally:
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - mirror JS top-level catch
        print(f"FATAL {error}", file=sys.stderr)
        sys.exit(1)
