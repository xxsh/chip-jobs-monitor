"""Shared helpers for HTTP job-source adapters: direct JSON fetch, HTML→text,
and the normalized job schema (identical shape to fetch.py's enriched jobs)."""

import json
import os
import re
import ssl
import urllib.request
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _build_ssl_context():
    # The python.org framework build ships no usable default CA store, so wire one up
    # explicitly: certifi if present (system python), else the macOS system bundle (.venv).
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem", "/usr/local/etc/openssl@3/cert.pem"):
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(path)
                    break
                except OSError:
                    continue
    return ctx


# Force direct connections (ignore proxy env): the pipeline runs proxy-stripped and
# these careers APIs are reachable directly. Pass a proxy explicitly if a source needs one.
_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_build_ssl_context()),
)


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def http_post_json(url, body, timeout=30):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"},
    )
    with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html):
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    text = unescape("".join(parser.parts))
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_items(html):
    """List items from <li> if present, else non-empty cleaned text lines."""
    if not html:
        return []
    items = re.findall(r"<li[^>]*>(.*?)</li>", html, re.S | re.I)
    if items:
        return [t for t in (html_to_text(i).strip() for i in items) if t]
    return [ln.strip() for ln in html_to_text(html).split("\n") if ln.strip()]


def iso_date(value):
    return str(value)[:10] if value else None


def iso_to_ts(value):
    if not value:
        return None
    text = str(value)
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return int(datetime.fromisoformat(candidate).timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def normalized_job(
    *,
    id,
    jr,
    name,
    locations,
    link,
    department=None,
    work_location_option=None,
    posted_ts=None,
    creation_ts=None,
    date_posted=None,
    valid_through=None,
    employment_type=None,
    description="",
    summary="",
    responsibilities=None,
    requirements=None,
    preferred=None,
    detail_error=None,
):
    """Build a job dict in the exact shape/key-order fetch.py emits for enriched jobs."""
    return {
        "id": id,
        "jr": jr,
        "name": name,
        "locations": [loc for loc in (locations or []) if loc],
        "department": department,
        "workLocationOption": work_location_option,
        "postedTs": posted_ts,
        "creationTs": creation_ts,
        "link": link,
        "datePosted": date_posted,
        "validThrough": valid_through,
        "employmentType": employment_type,
        "description": description or "",
        "summary": summary or "",
        "responsibilities": responsibilities or [],
        "requirements": requirements or [],
        "preferred": preferred or [],
        "detailError": detail_error,
    }
