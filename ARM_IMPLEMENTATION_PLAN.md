# Arm Job Monitor Implementation Plan

## Summary

Add Arm as a Python source adapter in the existing multi-company job monitor. Do not add `fetch-arm.mjs`; the project has already moved to Python source adapters under `sources/`.

Arm's official careers site is:

- Company site: `https://www.arm.com`
- Jobs site: `https://careers.arm.com/search-jobs`

Arm uses Radancy/TalentBrew. The Shanghai job list can be fetched through the search POST endpoint, and each job detail page exposes a JSON-LD `JobPosting` block with the full job description.

## Key Changes

1. Add `sources/arm.py`.

   - POST to `https://careers.arm.com/search-jobs/resultspost`.
   - Use the Shanghai city facet:

     ```json
     {
       "ID": "1814991-1796231-1796236",
       "FacetType": 4,
       "Count": 6,
       "Display": "Shanghai, Shanghai Municipality, China",
       "IsApplied": true,
       "FieldName": ""
     }
     ```

   - Parse returned `results` HTML for job cards:
     - `data-job-id`
     - title
     - detail URL
     - location
     - category

   - Fetch each job detail page and parse `<script type="application/ld+json">` where `@type == "JobPosting"`.
   - Normalize each job with `sources.base.normalized_job(...)`.

2. Register Arm in `sources/__init__.py`.

   Add:

   ```python
   from .arm import fetch_arm
   ```

   Add to `SOURCES`:

   ```python
   "arm": {
       "display": "Arm",
       "fetch": lambda max_jobs=None: fetch_arm(max_jobs=max_jobs),
   },
   ```

3. Reuse the existing Python daily pipeline.

   - `daily.py` already supports non-NVIDIA sources via the source registry.
   - Arm snapshots should be written as:

     ```text
     snapshots/{YYYY-MM-DD}_arm_shanghai-china.json
     snapshots/{YYYY-MM-DD}_arm_shanghai-china.md
     snapshots/{YYYY-MM-DD}_arm_shanghai-china.csv
     ```

   - Per-source reports should be written as:

     ```text
     reports/{YYYY-MM-DD}_arm_shanghai-china.json
     ```

4. Fix source-specific report metadata in `daily.py`.

   In the non-baseline `build_report(...)` return object, change:

   ```python
   "currentSnapshot": snapshot_filename(report_date),
   ```

   to:

   ```python
   "currentSnapshot": snapshot_filename(report_date, source),
   ```

   This also fixes the existing AMD metadata bug where AMD reports currently point to NVIDIA-style snapshot names.

5. Keep existing NVIDIA behavior unchanged.

   - NVIDIA remains fetched by `fetch.py`.
   - Existing NVIDIA filenames keep the old format without a source infix.
   - Do not remove `fetch.mjs` or `daily.mjs` in this change.

## Arm Adapter Details

The search request should use a POST body shaped like the existing Radancy client expects:

```json
{
  "ActiveFacetID": "1814991-1796231-1796236",
  "CurrentPage": 1,
  "RecordsPerPage": 15,
  "Distance": 50,
  "RadiusUnitType": 0,
  "Keywords": "",
  "Location": "",
  "Latitude": null,
  "Longitude": null,
  "ShowRadius": false,
  "IsPagination": "False",
  "CustomFacetName": "",
  "FacetTerm": "",
  "FacetType": 0,
  "FacetFilters": [
    {
      "ID": "1814991-1796231-1796236",
      "FacetType": 4,
      "Count": 6,
      "Display": "Shanghai, Shanghai Municipality, China",
      "IsApplied": true,
      "FieldName": ""
    }
  ],
  "SearchResultsModuleName": "Search Results",
  "SearchFiltersModuleName": "Search Filters",
  "SortCriteria": 0,
  "SortDirection": 1,
  "SearchType": 5,
  "CategoryFacetTerm": null,
  "CategoryFacetType": null,
  "LocationFacetTerm": null,
  "LocationFacetType": null,
  "KeywordType": null,
  "LocationType": null,
  "LocationPath": null,
  "OrganizationIds": "",
  "RefinedKeywords": [],
  "PostalCode": "",
  "ResultsType": 1
}
```

For each detail page:

- Prefer JSON-LD `JobPosting` fields:
  - `identifier` -> `jr`
  - search `data-job-id` -> `id`
  - `title` -> `name`
  - `url` -> `link`
  - `datePosted` -> `datePosted` and `postedTs`
  - `employmentType` -> `employmentType`
  - `description` -> HTML source for text extraction
  - `jobLocation[].address` -> `locations`
- Use detail page metadata as fallback when JSON-LD is incomplete:
  - `job-ats-req-id`
  - `job-tbcn-job-title`
  - `dimension6` for category
  - `.job-date`, `.job-location`, `.job-category`

## Description Parsing

Convert HTML to text with `html_to_text(...)`, then split common Arm headings:

- Summary:
  - `About the Role`
  - `Job Overview`
  - `Introduction`
- Responsibilities:
  - `Key Responsibilities`
  - `Impact`
  - role-specific subheadings under responsibilities
- Requirements:
  - `Required Qualifications`
  - `Who You Are`
- Preferred:
  - `Preferred Qualifications`
  - `Nice to Have`

Remove or ignore boilerplate sections from structured lists where practical:

- `Accommodations at Arm`
- `Hybrid Working at Arm`
- `Equal Opportunities at Arm`
- `10x mindset`

Keep the full cleaned text in `description` so the scorer still has complete context.

## Tests

Add focused Python tests.

1. `sources/arm.py` parsing tests:

   - Search-result HTML with six Shanghai cards parses into six jobs.
   - A detail page JSON-LD `JobPosting` parses into normalized schema fields.
   - Section parsing maps:
     - `Key Responsibilities` -> `responsibilities`
     - `Required Qualifications` -> `requirements`
     - `Preferred Qualifications` -> `preferred`

2. `daily_test.py` metadata test:

   - For `source="arm"`, `currentSnapshot` is `2026-05-23_arm_shanghai-china.json`.
   - For `source="nvidia"` or `None`, old NVIDIA naming remains unchanged.

## Verification Commands

Run from `nvidia-jobs-monitor/`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest daily_test
MONITOR_SOURCES=arm NVIDIA_SNAPSHOT_LABEL=daily-preview .venv/bin/python daily.py
```

Expected preview files:

```text
snapshots/daily-preview_arm_shanghai-china.json
snapshots/daily-preview_arm_shanghai-china.md
snapshots/daily-preview_arm_shanghai-china.csv
reports/daily-preview_arm_shanghai-china.json
reports/latest_shanghai-china.json
reports/latest_shanghai-china.md
reports/latest_shanghai-china_telegram.md
```

## Acceptance Criteria

- `sources.SOURCES` includes `arm`.
- `MONITOR_SOURCES=arm .venv/bin/python daily.py` fetches Arm Shanghai jobs without Playwright.
- Arm jobs include full job descriptions from JSON-LD.
- Source-prefixed snapshot and report files are generated.
- Existing NVIDIA and AMD runs remain compatible.
- Tests pass.

## Implementation Status

Implemented on 2026-05-23.

Files changed:

- `sources/arm.py`
- `sources/base.py`
- `sources/__init__.py`
- `daily.py`
- `daily_test.py`
- `test_arm_source.py`
- `package.json`
- `scripts/dashboard-server.mjs`
- `web/dashboard.js`

Additional fixes made during validation:

- Fixed `daily.py` source-specific `currentSnapshot` metadata for non-baseline reports.
- Fixed internship filtering to classify internships from role title/name only. Arm labels a non-intern `International Finance` manager role as `employmentType: Internship`, which previously caused a false auto-skip.
- Added Arm dashboard label/color wiring.
- Expanded `npm run daily:test` to include Arm source tests.

Verification run:

```bash
PYTHONDONTWRITEBYTECODE=1 npm run daily:test
PYTHONDONTWRITEBYTECODE=1 npm test
node --check scripts/dashboard-server.mjs
node --check web/dashboard.js
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile sources/base.py sources/arm.py sources/__init__.py daily.py daily_test.py test_arm_source.py
PYTHONDONTWRITEBYTECODE=1 python3 verify_daily.py
PYTHONDONTWRITEBYTECODE=1 python3 verify_db.py
```

Live Arm checks completed:

- Arm source fetch smoke test returned Shanghai jobs.
- `MONITOR_SOURCES=arm NVIDIA_SNAPSHOT_LABEL=daily-preview .venv/bin/python daily.py` wrote source-prefixed preview snapshot/report files.
- `NVIDIA_SNAPSHOT_LABEL=arm-seed-preview` seed validation scored six Arm jobs with zero scoring errors.
