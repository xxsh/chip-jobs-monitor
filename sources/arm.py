"""Arm careers adapter (Radancy/TalentBrew platform).

Arm's Shanghai search uses a JSON POST endpoint that returns job-card HTML.
Each detail page embeds a JSON-LD JobPosting with the full description.
"""

import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser

from .base import html_to_text, http_get_text, http_post_json, iso_to_ts, normalized_job

BASE_URL = "https://careers.arm.com"
SEARCH_URL = f"{BASE_URL}/search-jobs/resultspost"
RECORDS_PER_PAGE = 15
SHANGHAI_FACET = {
    "ID": "1814991-1796231-1796236",
    "FacetType": 4,
    "Count": 6,
    "Display": "Shanghai, Shanghai Municipality, China",
    "IsApplied": True,
    "FieldName": "",
}


def _classes(attrs):
    return set((attrs.get("class") or "").split())


def _clean(value):
    if value is None:
        return None
    text = unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


class _SearchResultsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jobs = []
        self._current = None
        self._depth = 0
        self._capture = None
        self._capture_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = _classes(attrs)
        if tag == "li" and "job-card" in classes:
            self._current = {"id": None, "title": None, "link": None, "location": None, "category": None}
            self._depth = 1
            return
        if self._current is None:
            return
        if tag == "li":
            self._depth += 1
        if tag == "a" and "job-card__title" in classes:
            self._current["id"] = attrs.get("data-job-id")
            self._current["link"] = urllib.parse.urljoin(BASE_URL, attrs.get("href") or "")
            self._start_capture("title")
        elif tag == "span" and "location" in classes:
            self._start_capture("location")
        elif tag == "span" and "category" in classes:
            self._start_capture("category")

    def handle_data(self, data):
        if self._capture:
            self._capture_parts.append(data)

    def handle_endtag(self, tag):
        if self._capture and tag in ("a", "span"):
            self._finish_capture()
        if self._current is not None and tag == "li":
            self._depth -= 1
            if self._depth <= 0:
                if self._current.get("link") and self._current.get("title"):
                    self.jobs.append(self._current)
                self._current = None
                self._capture = None

    def _start_capture(self, field):
        self._capture = field
        self._capture_parts = []

    def _finish_capture(self):
        if self._current is not None:
            self._current[self._capture] = _clean("".join(self._capture_parts))
        self._capture = None
        self._capture_parts = []


class _JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self._capture = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "application/ld+json" in (attrs.get("type") or "").lower():
            self._capture = True
            self._parts = []

    def handle_data(self, data):
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._capture:
            self.scripts.append("".join(self._parts).strip())
            self._capture = False
            self._parts = []


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = dict(attrs)
        key = attrs.get("name") or attrs.get("property")
        if key and attrs.get("content") is not None:
            self.meta[key] = _clean(attrs.get("content"))


def _search_payload(page):
    return {
        "ActiveFacetID": SHANGHAI_FACET["ID"],
        "CurrentPage": page,
        "RecordsPerPage": RECORDS_PER_PAGE,
        "Distance": 50,
        "RadiusUnitType": 0,
        "Keywords": "",
        "Location": "",
        "Latitude": None,
        "Longitude": None,
        "ShowRadius": False,
        "IsPagination": "True" if page > 1 else "False",
        "CustomFacetName": "",
        "FacetTerm": "",
        "FacetType": 0,
        "FacetFilters": [SHANGHAI_FACET],
        "SearchResultsModuleName": "Search Results",
        "SearchFiltersModuleName": "Search Filters",
        "SortCriteria": 0,
        "SortDirection": 1,
        "SearchType": 5,
        "CategoryFacetTerm": None,
        "CategoryFacetType": None,
        "LocationFacetTerm": None,
        "LocationFacetType": None,
        "KeywordType": None,
        "LocationType": None,
        "LocationPath": None,
        "OrganizationIds": "",
        "RefinedKeywords": [],
        "PostalCode": "",
        "ResultsType": 1,
    }


def _parse_search_results(html):
    parser = _SearchResultsParser()
    parser.feed(html or "")
    return parser.jobs


def _parse_meta(html):
    parser = _MetaParser()
    parser.feed(html or "")
    return parser.meta


def _find_job_posting(value):
    if isinstance(value, dict):
        typ = value.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if "JobPosting" in types:
            return value
        for child in value.values():
            found = _find_job_posting(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_job_posting(child)
            if found:
                return found
    return None


def _extract_job_posting(html):
    parser = _JsonLdParser()
    parser.feed(html or "")
    for script in parser.scripts:
        try:
            found = _find_job_posting(json.loads(script))
        except json.JSONDecodeError:
            continue
        if found:
            return found
    return {}


def _identifier_value(value):
    if isinstance(value, dict):
        for key in ("value", "name", "@id"):
            if value.get(key):
                return str(value[key])
        return None
    if value is None:
        return None
    return str(value)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _address_country(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("addressCountry")
    return value


def _location_from_address(address):
    if isinstance(address, str):
        return _clean(address)
    if not isinstance(address, dict):
        return None
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        _address_country(address.get("addressCountry")),
    ]
    location = ", ".join(part for part in (_clean(p) for p in parts) if part)
    return location or _clean(address.get("streetAddress"))


def _locations_from_posting(posting):
    locations = []
    for location in _as_list(posting.get("jobLocation")):
        if isinstance(location, dict):
            parsed = _location_from_address(location.get("address"))
            if parsed:
                locations.append(parsed)
        elif location:
            locations.append(_clean(location))
    return list(dict.fromkeys(loc for loc in locations if loc))


def _normalize_date(value):
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        first, second, year = (int(part) for part in match.groups())
        if second > 12:
            month, day = first, second
        else:
            day, month = first, second
        return f"{year:04d}-{month:02d}-{day:02d}"
    return text[:10] if len(text) >= 10 else text


_SECTION_ALIASES = {
    "summary": {"about the role", "job overview", "introduction"},
    "responsibilities": {"responsibilities", "key responsibilities", "impact"},
    "requirements": {
        "required qualifications",
        "required skills and experience",
        "skills and experience",
        "who you are",
    },
    "preferred": {
        "preferred qualifications",
        "nice to have",
        "nice to have skills and experience",
    },
}
_BOILERPLATE_HEADINGS = {
    "accommodations at arm",
    "hybrid working at arm",
    "equal opportunities at arm",
    "10x mindset",
    "in return",
}


def _heading_name(line):
    text = re.sub(r"^[•\-\*\s]+", "", line).strip().strip(":")
    text = re.sub(r"[“”\"']", "", text)
    text = re.sub(r"\s+", " ", text).lower()
    return text


def _section_for_heading(line):
    name = _heading_name(line)
    if name in _BOILERPLATE_HEADINGS:
        return "stop"
    for section, aliases in _SECTION_ALIASES.items():
        if name in aliases:
            return section
    return None


def _strip_bullet(line):
    return re.sub(r"^[•\-\*\s]+", "", line).strip()


def _join_wrapped(lines):
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _lines_to_items(lines):
    items = []
    current = []
    for line in lines:
        if re.match(r"^\s*(?:•|-|\*)\s+", line):
            if current:
                items.append(_join_wrapped(current))
            current = [_strip_bullet(line)]
        elif current:
            current.append(line)
        else:
            items.append(_join_wrapped([line]))
    if current:
        items.append(_join_wrapped(current))
    return [item for item in items if item]


def _split_description(text):
    sections = {"summary": [], "responsibilities": [], "requirements": [], "preferred": []}
    current = "summary"
    for line in (ln.strip() for ln in (text or "").splitlines()):
        if not line:
            continue
        section = _section_for_heading(line)
        if section == "stop":
            break
        if section:
            current = section
            continue
        sections[current].append(line)
    return {
        "summary": _join_wrapped(sections["summary"]),
        "responsibilities": _lines_to_items(sections["responsibilities"]),
        "requirements": _lines_to_items(sections["requirements"]),
        "preferred": _lines_to_items(sections["preferred"]),
    }


def _normalize_card(card, detail_html, detail_error=None):
    posting = _extract_job_posting(detail_html)
    meta = _parse_meta(detail_html)
    description = html_to_text(posting.get("description"))
    parsed = _split_description(description)
    date_posted = _normalize_date(posting.get("datePosted") or meta.get("dimension19"))
    link = posting.get("url") or card.get("link")
    employment_type = posting.get("employmentType")
    if isinstance(employment_type, list):
        employment_type = ", ".join(str(item) for item in employment_type if item)

    return normalized_job(
        id=str(card.get("id") or meta.get("search-analytics-currentJobId") or ""),
        jr=_identifier_value(posting.get("identifier")) or meta.get("job-ats-req-id") or card.get("id"),
        name=posting.get("title") or meta.get("job-tbcn-job-title") or card.get("title"),
        locations=_locations_from_posting(posting) or [card.get("location") or meta.get("dimension7")],
        department=meta.get("dimension6") or card.get("category"),
        work_location_option=None,
        posted_ts=iso_to_ts(date_posted or posting.get("datePosted")),
        creation_ts=None,
        date_posted=date_posted,
        employment_type=employment_type,
        description=description,
        summary=parsed["summary"],
        responsibilities=parsed["responsibilities"],
        requirements=parsed["requirements"],
        preferred=parsed["preferred"],
        link=urllib.parse.urljoin(BASE_URL, link or ""),
        detail_error=detail_error,
    )


def _fetch_detail_job(card):
    try:
        return _normalize_card(card, http_get_text(card["link"]))
    except Exception as exc:  # noqa: BLE001 - keep one failed detail page from killing the source
        return _normalize_card(card, "", str(exc))


def fetch_arm(max_jobs=None, detail_concurrency=6):
    cards = []
    page = 1
    while True:
        data = http_post_json(SEARCH_URL, _search_payload(page))
        batch = _parse_search_results(data.get("results") or "")
        cards.extend(batch)
        if len(batch) < RECORDS_PER_PAGE:
            break
        if max_jobs and len(cards) >= max_jobs:
            break
        page += 1
        if page > 20:
            break

    if max_jobs:
        cards = cards[:max_jobs]

    workers = max(1, min(detail_concurrency, len(cards) or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        jobs = list(executor.map(_fetch_detail_job, cards))
    jobs.sort(key=lambda job: job.get("postedTs") or 0, reverse=True)
    return jobs
